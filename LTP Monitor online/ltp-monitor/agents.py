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
import store
import re
import threading
import time
import urllib.request
import urllib.error
from collections import deque
from datetime import datetime, timedelta, timezone

import config
import daily_marks
from analyzer import analyze, ai_signal, ai_budget_status as _ai_budget

try:
    import dhan_ws
except Exception as _e:
    dhan_ws = None
    print(f"[agents] dhan_ws unavailable, websocket market data disabled: {_e}")

try:
    import dhan_scrip_master
except Exception as _e:
    dhan_scrip_master = None
    print(f"[agents] dhan_scrip_master unavailable, futures OI disabled: {_e}")

try:
    from news_macro_agent import NewsMacroAgent
except Exception as _e:
    # Optional feature module — degrade loudly (printed at import time,
    # not silently swallowed) rather than crashing the whole app if the
    # global-macro data module has an issue.
    NewsMacroAgent = None
    print(f"[agents] news_macro_agent unavailable, NewsMacroAgent disabled: {_e}")

try:
    from telegram_bot import TelegramAgent
except Exception as _e:
    TelegramAgent = None
    print(f"[agents] telegram_bot unavailable, TelegramAgent disabled: {_e}")

try:
    from marketsense_link import MarketSenseAgent
except Exception as _e:
    # Optional read-only bridge to the MarketSense platform (separate
    # process, :8100). Same degrade-loudly pattern as NewsMacroAgent.
    MarketSenseAgent = None
    print(f"[agents] marketsense_link unavailable, MarketSenseAgent disabled: {_e}")

IST = timezone(timedelta(hours=5, minutes=30))
BASE = os.path.dirname(os.path.abspath(__file__))
# Persist trade history + logs in the user's home dir so a code-folder
# update / re-zip never wipes them.
STORE_DIR = store.home()
os.makedirs(STORE_DIR, exist_ok=True)
JOURNAL = os.path.join(STORE_DIR, "journal.json")
WEEKLY_RISK_JOURNAL = os.path.join(STORE_DIR, "weekly_risk_journal.json")


def _dedupe_journal_file(path, key, log=None):
    """2026-07-28 — one-time cleanup for duplicate journal entries
    accumulated BEFORE the journal_done-vs-restart fix existed (see
    DailyJournalAgent/WeeklyRiskAgent's cycle()). journal_done/
    weekly_risk_done were always in-memory-only bus flags that never
    survived a restart, while the journal files themselves DO persist
    — every restart after 15:35 IST on a given day re-ran that day's
    journal write and appended another duplicate entry rather than
    recognizing one already existed. Confirmed directly from a live
    file: some dates had up to 8 identical duplicate entries.

    Keeps the LAST entry per unique `key` value (a later same-day
    duplicate could have captured a trade that closed after an earlier
    run already wrote) and preserves original first-seen ordering.
    Returns (original_count, deduped_count) — equal counts means
    nothing needed cleaning (a no-op on every run after the first).
    """
    if not os.path.exists(path):
        return (0, 0)
    try:
        data = json.load(open(path))
    except Exception as e:
        if log:
            log(f"\u26a0 journal dedup migration failed for "
                f"{os.path.basename(path)}: {type(e).__name__}: {e}")
        return (0, 0)
    seen_order = []
    by_key = {}
    for entry in data:
        k = entry.get(key)
        if k not in by_key:
            seen_order.append(k)
        by_key[k] = entry   # last occurrence wins
    deduped = [by_key[k] for k in seen_order]
    if len(deduped) != len(data):
        json.dump(deduped, open(path, "w"), indent=2, default=str)
        if log:
            log(f"one-time cleanup: {os.path.basename(path)} had "
                f"{len(data)} entries for {len(deduped)} unique {key}s "
                f"(duplicates from restarts before a fix) — deduped, "
                f"keeping the last entry per {key}")
    return (len(data), len(deduped))

TRADES_FILE = os.path.join(STORE_DIR, "trades.jsonl")   # append-only, one JSON per line
OPEN_STATE_FILE = os.path.join(STORE_DIR, "open_state.json")  # snapshot of open positions+spreads
LOG_FILE = os.path.join(STORE_DIR, "activity.log")


def _save_open_state(positions: dict, spreads: dict):
    """Snapshot currently-open positions/spreads to disk so a restart
    (e.g. to apply an update) doesn't silently lose track of them —
    unlike trades.jsonl (append-only, closed trades only), this is a
    full overwrite since open state mutates constantly. Written
    atomically (tmp file + rename) so a crash mid-write can't corrupt
    it and lose everything."""
    try:
        os.makedirs(STORE_DIR, exist_ok=True)
        tmp = OPEN_STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"positions": positions, "spreads": spreads,
                      "saved_at": now_ist().strftime("%Y-%m-%d %H:%M:%S")},
                     f, default=str)
        os.replace(tmp, OPEN_STATE_FILE)
    except Exception as e:
        print(f"[persist] failed to save open state: {e}")


def load_open_state():
    """Restore open positions/spreads on startup. Returns (positions,
    spreads), both {} if no snapshot exists or it's corrupt — a
    corrupt/missing snapshot should never crash startup, just start
    with no known open trades (same as before this feature existed)."""
    if not os.path.exists(OPEN_STATE_FILE):
        return {}, {}
    try:
        with open(OPEN_STATE_FILE) as f:
            d = json.load(f)
        return d.get("positions", {}) or {}, d.get("spreads", {}) or {}
    except Exception as e:
        print(f"[persist] failed to load open state: {e}")
        return {}, {}


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


def _record_closed(bus, closed):
    """Append to the in-memory closed_trades window, capped (v59.71,
    third-eye Tier 4). The list is re-scanned by five consumers per
    cycle and previously grew for the process lifetime; the FULL history
    lives in trades.jsonl (written by _append_trade at every call site
    of this), the bus carries the working window."""
    import config as _cfg
    trades = bus.get("closed_trades", [])
    trades.append(closed)
    cap = int(_cfg.load().get("closed_trades_memory_cap", 5000) or 5000)
    bus.set("closed_trades", trades[-cap:])


def _append_activity(line: str):
    """Append a single activity-log line to disk (best-effort).

    v59.71 (third-eye Tier 4) — size-capped: rotates to activity.log.1
    past ~10 MB, one generation kept. The live file had reached 12 MB
    with the operator rotating it by hand."""
    try:
        try:
            if os.path.getsize(LOG_FILE) > 10 * 1024 * 1024:
                os.replace(LOG_FILE, LOG_FILE + ".1")
        except OSError:
            pass
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def now_ist():
    return datetime.now(IST)


def in_market_session(ts):
    """True if `ts` (unix epoch seconds) falls inside NSE/BSE trading
    hours (Mon-Fri, 09:15-15:30 IST, with a few minutes' tail for the
    closing auction).

    2026-07-27 — moved here from app.py's own `_in_market_session` (a
    near-identical function) to be the SINGLE shared definition rather
    than risk the exact "two copies that silently drift" bug already
    found and fixed once this session (news_engine.py/news_macro_
    agent.py's duplicate bearish/bullish word regexes) — `history.py`'s
    new candle-pruning function (2026-07-27) needs the IDENTICAL
    definition already proven correct for the read-side filter that's
    been protecting the indicator/chart paths since 2026-07-26; a
    second, parallel implementation could disagree at the margins
    (weekday boundary, closing-auction tail) with nothing to catch it.
    app.py's own `_in_market_session` is now a thin wrapper around this.
    """
    if ts is None:
        return False
    t = datetime.fromtimestamp(ts, IST)
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    # +5 on the close for the auction tail. 2026-08-03: the F&O close
    # moved 15:30 -> 15:40, so a 15:35 bound was silently DISCARDING
    # real bars at the candle write gate.
    return _session_min("fno_open_time", "09:15") <= hm <= \
        _session_min("fno_close_time", "15:40") + 5


def _session_min(key, default):
    """Config "HH:MM" -> minutes past midnight IST, clamped to a day."""
    try:
        hh, mm = str(config.load().get(key) or default).split(":")
        v = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError, TypeError):
        hh, mm = default.split(":")
        v = int(hh) * 60 + int(mm)
    return max(0, min(24 * 60 - 1, v))


def strip_cas_frozen(candles):
    """Drop bars from the closing call-auction window, for INDICATOR use.

    2026-08-04. From the NSE change effective 2026-08-03, F&O stocks stop
    trading continuously at 15:15 and enter the closing call auction.
    Every NIFTY/BANKNIFTY/FINNIFTY constituent IS an F&O stock, so from
    that minute the INDEX has nothing left to discover: it repeats its
    last value until the auction publishes the official close, which
    arrives as a single step (~150 points on 2026-08-04, 15:28).

    This was verified against the BROKER rather than inferred from our own
    archive, because our archive could not distinguish "the market froze"
    from "we stopped collecting". Dhan returns 0 flat 1m index bars for
    15:15-15:30 on 2026-07-30 and 30 of 32 on 2026-08-03 — the first
    session under the new rules.

    WHAT THIS DOES NOT DO: it does not stop the bars being stored. They
    are real broker data and the official close is genuinely useful; the
    chart and the archive keep them. What they must not do is reach an
    INDICATOR, because ~13 identical bars followed by a 150-point gap is
    read by ATR as a volatility spike, by MACD/EMA as a cross, and by the
    ZigZag as a pivot — none of which traded. ATR matters most: it feeds
    `option_stop_geometry`, so a frozen-index artifact would set real
    stop widths.

    Futures keep trading to 15:40 and are NOT frozen, so this is
    deliberately applied only where index candles feed indicators.
    """
    cutoff = _session_min("cas_freeze_time", "15:15")
    out = []
    for c in candles or []:
        ts = c.get("ts") if isinstance(c, dict) else None
        if ts is None:
            out.append(c)
            continue
        t = datetime.fromtimestamp(int(ts), IST)
        if t.hour * 60 + t.minute < cutoff:
            out.append(c)
    return out


def market_open():
    """May we TRADE or HOLD an intraday position right now?

    2026-08-03 — NSE split what used to be one boundary into two. Index
    F&O trades until 15:40, but INTRADAY F&O is auto-squared by the
    broker at 15:25. This function keeps its original meaning — "may I
    be in a position" — and therefore now closes at `fno_squareoff_time`
    (15:22, a 3-minute margin), NOT at the market close.

    That is deliberately the CONSERVATIVE half of the split. Both EOD
    square-off branches and every entry gate call this, so any call site
    not individually reviewed defaults to standing down EARLY rather
    than holding past the broker's square-off — the failure that costs
    real money. Data-only callers that genuinely want the full session
    use `fno_session_open()` instead.
    """
    t = now_ist()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return _session_min("fno_open_time", "09:15") <= hm <= \
        _session_min("fno_squareoff_time", "15:22")


def fno_session_open():
    """Is the F&O MARKET open — i.e. is data still arriving? 09:15-15:40.

    Distinct from market_open(): between the intraday square-off and the
    close the market is live and ticking, but we must be flat. Data
    collection should continue through that window; trading must not.
    """
    t = now_ist()
    if t.weekday() >= 5:
        return False
    hm = t.hour * 60 + t.minute
    return _session_min("fno_open_time", "09:15") <= hm <= \
        _session_min("fno_close_time", "15:40")


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

def symbol_paused(symbol, cfg=None):
    """True when `symbol` is on hold: DATA continues, ENTRIES stop.

    2026-08-06. Measured across 292 closed trades, BANKNIFTY was the
    worst symbol in BOTH regimes — -Rs 40,781 at a 28% win rate all
    time, and 0-for-3 for -Rs 768 since the per-trade caps came in. Held
    on explicit instruction.

    A hold is deliberately NOT "remove it from the bus symbols list".
    That list drives market data, analysis, regime, chain snapshots and
    the archive as well as trading, so dropping a name there would stop
    collecting the very evidence needed to decide whether the hold was
    right. Signals are still generated and logged; only the ORDER is
    refused, and the shadow record keeps accruing.

    EXITS ARE NEVER BLOCKED. This is checked on entry paths only — a
    pause that could strand an open position would be a far worse bug
    than the losses it is meant to prevent.
    """
    if not symbol:
        return False
    c = cfg if cfg is not None else config.load()
    paused = c.get("paused_symbols") or []
    return str(symbol).upper() in {str(x).upper() for x in paused}


def realized_pnl_today(bus):
    """Sum of TODAY's closed-trade net P&L — the ONE definition of "day
    P&L" for risk gating.

    v59.69 (third-eye Tier 3) — RiskAgent used to keep an incremental
    `self.daily_pnl` that was (a) never reset on date rollover, so the
    "daily" loss limit was actually a since-restart limit, and (b) zeroed
    by any restart while open_state.json re-seeded the open positions —
    a mid-day restart forgot the morning's realized losses entirely
    (July 30 ended at 7.9× the daily limit partly on this). Deriving the
    figure from `closed_trades` — loaded from trades.jsonl at startup and
    appended on every close — is correct across restarts AND rollovers
    by construction, with nothing to remember to reset.
    """
    today = now_ist().strftime("%Y-%m-%d")
    return sum((t.get("pnl") or 0) for t in (bus.get("closed_trades") or [])
               if (t.get("closed_date") or (t.get("closed_at") or "")[:10]) == today)


def spread_exit_value(credit, pnl_ps):
    """The spread's VALUE at exit — the `premium_out` the cost model needs.

    P&L/share = credit − value  ⇒  value = credit − pnl_ps, floored at 0
    (a defined-risk spread's value cannot be negative).

    v59.68 (third-eye Tier 0) — the live exit passed pnl_per_share ITSELF
    as the exit premium, so on a losing spread the cost model received a
    NEGATIVE sell notional and STT became a rebate: the live charge on a
    loser ran ~20% below what the backtester charged for the identical
    trade (₹224 vs ₹280 on a credit-150 loser at −60/share), and the two
    disagreed on every spread. ONE definition now, called by the live
    exit and both backtester replays — the same collapse
    spread_exit_reason() below went through on 2026-08-06.
    """
    return max(0.0, float(credit or 0) - float(pnl_ps or 0))


def spread_exit_reason(sp, pnl_ps, spot, cfg, now_ts, market_is_open,
                       log=lambda m: None, alert=lambda *a: None):
    """The spread exit decision — ONE definition, used live AND in replay.

    2026-08-06. `backtester.replay_spreads` modelled four exits (profit
    target, loss limit, strike breach, EOD) while the live monitor also
    ran the defense zone, the per-share profit-lock ratchet,
    `rupee_profit_floor` and the time stop. `rupee_profit_floor` was
    referenced 8 times in agents.py and ZERO times in backtester.py.

    The two therefore measured DIFFERENT STRATEGIES, and the numbers
    said so on the same days' data:

        FINNIFTY bull_put   backtest n=15  -3,081  47% win
                            LIVE     n=17  +4,702  82% win

    Every promotion decision, auto-tuner sweep and "the backtest says
    this works" claim for spreads inherited that. A backtest that cannot
    reproduce live behaviour is not evidence about live behaviour.

    MUTATES `sp` — the defense tighten, the profit-lock ratchet and
    rupee_profit_floor all ratchet state forward, and they must advance
    exactly once per evaluation. That is why this is one function called
    once rather than a set of predicates called ad hoc.

    `now_ts` and `market_is_open` are parameters rather than
    time.time()/market_open() calls precisely so a replay can drive
    historical timestamps through the identical code.
    """
    # --- market closed: square off, evaluate NOTHING ------------------
    # v59.69 (third-eye Tier 3). This check used to sit LAST, below the
    # profit-target/loss-limit/breach chain — so after the session ended,
    # any non-zero last-traded premiums still in the bus kept the whole
    # chain live, and on 2026-07-30 three spreads "hit profit target" /
    # "loss limit" at 23:52:46 on phantom night-time prices. A closed
    # market has exactly one legitimate action, the square-off, and it
    # must not be preceded by decisions that pretend fills are available.
    # (Replay parity holds automatically: the final frame of a replay day
    # now labels as square-off rather than whichever branch fired first.)
    if not market_is_open:
        return "market closing — squaring off spread"
    # --- defense zone: act BEFORE a full breach, not only at it -------
    if cfg.get("spread_defense_enabled", True) and spot and not sp.get("defended"):
        short_leg = sp["legs"][0]["leg"]
        dist = (spot - sp["short_strike"] if short_leg == "PE"
                else sp["short_strike"] - spot)
        zone = sp["width"] * cfg.get("spread_defense_zone_pct", 30) / 100
        if 0 < dist <= zone:
            old_limit = sp["loss_limit"]
            sp["loss_limit"] = round(
                old_limit * cfg.get("spread_defense_tighten_pct", 50) / 100, 2)
            sp["defended"] = True
            log(f"🛡 {sp['symbol']} {sp['strategy']} defense triggered — "
                f"spot {spot:.0f} within {dist:.0f}pts of short strike "
                f"{sp['short_strike']:.0f}, loss limit tightened "
                f"₹{old_limit:.1f} → ₹{sp['loss_limit']:.1f}")
            alert("medium", sp["symbol"],
                  f"{sp['strategy']} defense: short strike approached, "
                  f"stop tightened to ₹{sp['loss_limit']:.1f}")

    # Evaluated ONCE — it advances a ratchet, so calling it twice inside
    # the chain below would double-advance it.
    _rpf_spread = rupee_profit_floor(sp, pnl_ps * sp["qty"], cfg, "spread")

    lock_trigger = sp["profit_target"] * cfg.get("spread_profit_lock_trigger_pct", 80) / 100
    if pnl_ps >= lock_trigger:
        candidate_floor = round(pnl_ps * cfg.get("spread_profit_lock_pct", 75) / 100, 2)
        if candidate_floor > sp.get("profit_floor", 0):
            sp["profit_floor"] = candidate_floor

    if pnl_ps >= sp["profit_target"]:
        return f"captured ₹{pnl_ps:.1f} of ₹{sp['credit']} credit"
    # v59.73 (third-eye Tier 2) — the floor must clear the ROUND TRIP,
    # not just a fixed ₹250: an exit that banks less than its own costs
    # is a donation with a green P&L cell. Modelled with the same
    # options_costs table the exit will actually be charged with, so
    # the arming bar tracks contract size and premium level.
    _lock_min = cfg.get("spread_profit_lock_min_rupees", 250)
    try:
        import options_costs as _oc
        _rt = _oc.cost_round_trip(
            sp["credit"],
            spread_exit_value(sp["credit"], sp.get("profit_floor", 0)),
            int(sp.get("qty") or 1), legs=2, cfg=cfg)["total"]
        _lock_min = max(_lock_min,
                        _rt * float(cfg.get("exit_min_cost_coverage", 1.0)
                                    or 0.0))
    except Exception:
        pass          # cost model unavailable → the fixed ₹ floor still holds
    if sp.get("profit_floor", 0) > 0 and pnl_ps <= sp["profit_floor"]             and (sp["profit_floor"] * sp["qty"]) >= _lock_min:
        # Absolute-₹ guard: the floor must be worth exiting for. Without
        # it the ratchet fired on ₹0.1-2/share peaks where fees exceeded
        # the entire gain — 26 such exits netted ₹62 total across the
        # 2026-07-16..23 live data.
        return (f"profit lock: gave back to floor ₹{sp['profit_floor']:.1f}/sh "
                f"(₹{sp['profit_floor'] * sp['qty']:.0f}) "
                f"after peaking near ₹{sp.get('mfe', 0) / sp['qty']:.1f}/sh")
    if _rpf_spread:
        return _rpf_spread
    if pnl_ps <= -sp["loss_limit"]:
        return f"loss limit (₹{pnl_ps:.1f} vs -₹{sp['loss_limit']})"
    if spot and ((sp["legs"][0]["leg"] == "PE" and spot < sp["short_strike"]) or
                 (sp["legs"][0]["leg"] == "CE" and spot > sp["short_strike"])):
        return f"short strike breached (spot {spot:.0f})"
    if cfg.get("time_stop_minutes", 0) and sp.get("opened_ts") and \
            (now_ts - sp["opened_ts"]) / 60 >= cfg["time_stop_minutes"]:
        elapsed = (now_ts - sp["opened_ts"]) / 60
        return (f"time stop ({elapsed:.0f}m ≥ {cfg['time_stop_minutes']}m) "
                f"— forcing a decision rather than waiting indefinitely")
    return None


def paper_order_id():
    """A paper order id that is actually unique.

    2026-08-06. It was `f"PAPER-{int(time.time())}"` — SECOND
    resolution — so any two positions opened in the same second shared
    one. Found while purging duplicate journal closes: keying on
    order_id alone reported four duplicate groups, and two of them were
    not duplicates at all but DIFFERENT INSTRUMENTS sharing an id:

        PAPER-1785143942 -> SENSEX + FINNIFTY + NIFTY futures
        PAPER-1785144544 -> NIFTY + SENSEX futures

    all opened 2026-07-27 14:49:02 and 14:59:04 respectively. A filter
    that trusted the id would have deleted five genuine trades.

    The uuid suffix also distinguishes ids minted by DIFFERENT
    PROCESSES, which is not hypothetical: on 2026-08-06 a restart left
    the previous process alive for three minutes and both wrote to the
    same journal. A pid would cover that case; a uuid covers it and
    restart-within-the-same-second too, without carrying state.

    The epoch prefix is kept so ids still sort chronologically.
    """
    import uuid
    return f"PAPER-{int(time.time())}-{uuid.uuid4().hex[:8]}"


def realistic_costs(kind, symbol, lots, premium_in, premium_out, cfg,
                    legs=1, log=None):
    """{"fees", "slippage", "total"} — statutory charges kept SEPARATE
    from the cost of crossing the bid-ask.

    2026-08-06, on being asked why a NIFTY round trip cost Rs 133 when
    fee_per_lot is 30. It breaks down as Rs 68.17 statutory + Rs 65.00
    bid-ask, and only the first is money a broker debits:

        brokerage  Rs 20 x 2 orders            = 40.00
        STT        0.10% x sell notional       =  9.51
        exchange   0.05% x both sides          =  9.45
        stamp      0.003% x buy notional       =  0.28
        GST        18% x (brokerage+exch+sebi) =  8.90
        ------------------------------------------------
        bid-ask    0.5 pts x 65 lot x 2 txns   = 65.00

    Reporting the sum as "fees" overstates what was charged. The
    bid-ask is nonetheless a REAL cost this system would otherwise miss
    entirely, because fills are recorded at LTP — between bid and ask —
    so crossing the spread never appears in the fill price. Hence: both
    are booked, under their own names.

    P&L is unchanged: pnl = gross - fees - slippage, and fees +
    slippage is exactly what the previous single figure was.
    """
    r = _cost_parts(kind, symbol, lots, premium_in, premium_out, cfg,
                    legs=legs, log=log)
    return r


def realistic_fees(kind, symbol, lots, premium_in, premium_out, cfg,
                   legs=1, log=None):
    """Total round-trip cost. Thin wrapper over realistic_costs() for
    callers that only need one number (the replays)."""
    return _cost_parts(kind, symbol, lots, premium_in, premium_out, cfg,
                       legs=legs, log=log)["total"]


def _cost_parts(kind, symbol, lots, premium_in, premium_out, cfg,
                legs=1, log=None):
    """Round-trip cost in rupees, notional/premium-aware.

    2026-08-06. Every live P&L figure charged `fee_per_lot * lots * N` —
    a flat per-lot amount that ignores what was actually traded. The
    Futures Research page had already measured what that costs:

        S11 momentum, NIFTY, 325 trades, SAME signals and exits
          flat fee_per_lot=40   +Rs 63,531   "would have been deployed"
          notional-aware model  -Rs 31,000   "no edge above costs"

    and the cost readout puts the OPTION understatement at Rs 26-47 per
    round trip per symbol, because the flat model omits the bid-ask
    spread entirely — the single largest component for an ATM index
    option.

    The correct models ALREADY EXISTED (`options_costs.py`,
    `futures_costs.py`) and were used by the promotion gate and the
    research page, but NOT by the live P&L or the backtester. This wires
    the live path to them rather than adding a third implementation.

    Falls back to the flat model if the real one cannot be computed —
    a missing premium must not make a trade look free.
    """
    lots = max(1, int(lots or 1))
    lot = (cfg.get("lot_sizes") or {}).get(symbol, 75)
    try:
        if kind == "future":
            import futures_costs
            # v59.68 (third-eye Tier 0) — this used to call
            # cost_round_trip(premium_in, premium_out, lot) POSITIONALLY
            # into a (symbol, entry, exit_px, ...) signature: the entry
            # price landed in `symbol`, .upper() raised AttributeError,
            # the except below swallowed it, and EVERY live/paper futures
            # round trip fell back to the flat model — ₹80 charged where
            # the notional model says ~₹602 (7.5x understatement), for
            # the exact defect futures_costs.py's own header was written
            # to prevent. breakdown() is used instead of cost_round_trip()
            # because this caller needs the statutory/slippage split and
            # cost_round_trip returns a single float (a second reason the
            # old call could never have worked).
            # v59.72 (R2 findings L4 + verifier) — breakdown(lots=lots)
            # scales correctly by construction: brokerage is per ORDER
            # and charged once; notional charges scale with qty. The old
            # per-1-lot × lots multiplied brokerage and the tax on it by the lot count
            # (~₹330 overcharge at 8 lots) and disagreed with
            # futures_costs.cost_round_trip on the same trade. Contract
            # size comes from the scrip master (the authority its own
            # docstring mandates); the config map is the fallback, said
            # out loud, when the master is unreadable.
            try:
                b = futures_costs.breakdown(symbol, float(premium_in),
                                            float(premium_out), lots=lots,
                                            cfg=cfg)
            except Exception:
                if log:
                    log(f"scrip master unavailable for {symbol} lot size — "
                        f"using config lot {lot} for the cost model")
                b = futures_costs.breakdown(symbol, float(premium_in),
                                            float(premium_out), lots=lots,
                                            cfg=cfg, lot=lot)
            stat = float(b["statutory_rupees"])
            slip = float(b["items"]["slippage"])
            if lots > 1:
                # v59.69 square-root size impact — the EXTRA walk into
                # the book per lot, applied to the (already lot-scaled)
                # linear slippage.
                _alpha = max(0.0, min(1.0, float(
                    cfg.get("slippage_impact_alpha", 0.5) or 0.0)))
                if _alpha > 0:
                    slip *= lots ** _alpha
            if stat + slip > 0:
                return {"fees": round(stat, 0), "slippage": round(slip, 0),
                        "total": round(stat + slip, 0), "model": "notional"}
        else:
            import options_costs
            # v59.69 (third-eye Tier 3) — size-aware spread. The
            # bid-ask does NOT scale linearly with size: the measured
            # distribution (median 0.65, mean 2.27, max 15.80 pts —
            # size_aware_cost.py) is thin depth at the touch, so a
            # multi-lot order walks the book. Modelled as
            # halfspread(n) = halfspread_1 x n^alpha (alpha 0.5, the
            # standard square-root impact form; 0 disables). Statutory
            # charges stay linear — they genuinely are.
            _kw = {}
            if lots > 1:
                _alpha = max(0.0, min(1.0, float(
                    cfg.get("slippage_impact_alpha", 0.5) or 0.0)))
                if _alpha > 0:
                    # v59.72 (R2 finding M4 sub-item) — `or 0.5` used to
                    # clobber a DELIBERATE opt_halfspread_points of 0
                    # (spread modelling off) back to 0.5 for multi-lot
                    # only. None → default; 0 → honoured (no impact on
                    # a zero spread).
                    _hs1_raw = cfg.get("opt_halfspread_points", 0.5)
                    _hs1 = 0.5 if _hs1_raw is None else float(_hs1_raw)
                    if _hs1 > 0:
                        _kw["halfspread"] = _hs1 * (lots ** _alpha)
            r = options_costs.cost_round_trip(
                float(premium_in), float(premium_out), lot,
                legs=legs, cfg=cfg, **_kw)
        stat = float(r.get("statutory", 0)) * lots
        if lots > 1:
            # v59.72 (R2/verifier) — brokerage is per ORDER, not per
            # lot: a 5-lot spread is still one order per leg per side.
            # Scaling the whole 1-lot statutory by lots over-charged
            # the fixed component by (lots−1)×; options_costs owns the
            # rate table, so the amount comes from there, not from a
            # re-declared constant here.
            stat = max(0.0, stat - (lots - 1)
                       * options_costs.fixed_order_cost(cfg, legs=legs))
        slip = float(r.get("spread", 0)) * lots
        if stat + slip > 0:
            # "model" makes a silent fallback OBSERVABLE downstream: the
            # trade record shows which cost model actually priced it.
            return {"fees": round(stat, 0), "slippage": round(slip, 0),
                    "total": round(stat + slip, 0), "model": "notional"}
    except Exception as e:
        if log:
            log(f"cost model unavailable for {symbol} ({type(e).__name__}) "
                f"- falling back to the flat fee_per_lot model, which "
                f"UNDERSTATES the real cost")
    # The fallback must never be CHEAPER than the shipped default. An
    # operator can legitimately tune fee_per_lot down; they cannot
    # legitimately make a trade free, and this path runs precisely when
    # the real model could not be computed — the worst moment to
    # understate. Erring high is recoverable; erring to zero is what
    # produced 184 zero-cost trades.
    import config as _cfg
    flat = max(float(cfg.get("fee_per_lot") or 0),
               float(_cfg.DEFAULTS.get("fee_per_lot", 40)))
    # The flat model has no notion of a spread, so all of it is booked
    # as fees rather than inventing a slippage split.
    t = round(flat * lots * 2 * max(1, legs), 0)
    return {"fees": t, "slippage": 0.0, "total": t, "model": "flat-fallback"}


def instant_exit_reason(pos, ltp, spot):
    """The exit conditions that can already be TRUE at the moment of
    entry — evaluated against LIVE data, returning a reason or None.

    2026-08-06, fourth instance of one family in a single session. Each
    time, the ENTRY used stale context from the 60s analysis pack while
    the EXIT checked live data at 3s:

        target2 unvalidated        -> `ltp >= target2` on the 1st check
        option_ltp from the pack   -> fill stale vs a live exit check
        spot_invalidation from it  -> SENSEX 78800 CE, 11:43:02, opened
                                      and closed in the SAME SECOND at
                                      78756 vs an invalidation of
                                      78791.6 — already breached BEFORE
                                      the position existed

    A position that would exit on its first monitor cycle should never
    be opened. This is the general guard rather than a third special
    case.

    ONLY the pure price-level predicates live here. The others in
    _monitor_one's chain CANNOT fire at entry and their exclusion is
    deliberate, not an oversight:

        transaction stop/target, step-trail, profit floor  pnl == 0
        time stop                                          elapsed == 0
        EOD square-off                                     not an entry
                                                           condition
        gave-back-after-T1                                 t1_hit False

    dynamic_exit_reason() is excluded for a different reason: it MUTATES
    ratchet state, so calling it here would double-advance it.

    _monitor_one() delegates its three price-level branches to this
    function, so there is ONE definition rather than two that drift —
    the failure this codebase has already had with the market-session
    check, the news regexes and the OI quadrant classifier.
    """
    if ltp is None:
        return None
    sl = pos.get("stoploss")
    if sl is not None and ltp <= sl:
        init = pos.get("initial_sl")
        entry = pos.get("entry")
        if entry is not None and sl > entry:
            return (f"trailing stop in profit (₹{ltp} ≤ ₹{sl}, "
                    f"locked above entry ₹{entry})")
        if init is not None and sl > init:
            return f"trailing stop (₹{ltp} ≤ ₹{sl}, raised from ₹{init})"
        return f"stoploss (₹{ltp} ≤ ₹{sl})"
    t2 = pos.get("target2")
    if t2 is not None and ltp >= t2:
        return f"target-2 (₹{ltp})"
    inv = pos.get("spot_invalidation")
    if inv and spot:
        leg = pos.get("leg")
        if (leg == "CE" and spot < inv) or (leg == "PE" and spot > inv):
            return f"spot invalidation ({spot:.0f} vs {inv})"
    return None


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
            if key in ("positions", "spreads"):
                _save_open_state(self.state.get("positions", {}) or {},
                                 self.state.get("spreads", {}) or {})

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

def rupee_profit_floor(state, pnl_rupees, cfg, label="position"):
    """Shared RUPEE-denominated profit ratchet — options, spreads, futures.

    2026-07-29, from a full live session: ₹55,707 of peak unrealised
    profit became ₹24,429 realised. ₹31,278 given back, 43.9% capture,
    20 of 31 trades surrendering something. The cause was not a missing
    mechanism but the WRONG UNIT on every mechanism that existed:

      * spread profit-lock armed at `profit_target x 80%`. It armed on
        2 of 11 spreads — both of which reached target anyway. One
        peaked at ₹11.40/sh against an arming level of ₹12.08 (missed
        by 6%) and then gave back ₹2,875. A lock that arms at 80% of
        target can only protect trades already about to hit target.
      * single-leg `trail_sl_trigger_pct` = 5% of premium. Today's
        big-giveback options peaked at 0.6% / 1.0% / 1.5% / 4.4%.
        Never armed.
      * `futures_trail_trigger_pct` = 0.3% of PRICE. On a 57,320 entry
        that is a 172-point move; the best futures position peaked at
        138 points. Never armed. A percentage of notional cannot
        describe an intraday futures profit.

    Meanwhile every RUPEE-denominated exit captured its full peak:
    three `transaction_target_rupees` exits with ZERO giveback, and the
    one `step_trail` exit that fired. The unit was the whole problem.

    So: one ratchet, in rupees, identical for all three instrument
    types. Arms at an absolute P&L, keeps a fraction of the peak, rises
    with new peaks and never falls. `min_rupees` stops it firing on
    peaks so small that fees exceed the gain — the same guard the
    spread lock already carries, for the same reason.

    Mutates `state` (the position/spread dict) and returns an exit
    reason string, or None. The caller decides where in its own
    precedence chain to consult it — but it MUST sit before the blunt
    instruments (time stop, EOD square-off, kill-switch), which are
    exactly what closed today's give-backs.
    """
    if not cfg.get("rupee_profit_floor_enabled", True):
        return None
    # v58.39 — per-class settings. v58.35 applied ONE ratchet uniformly,
    # which is right for a directional buy and wrong for a credit
    # spread: a spread decaying normally toward max credit routinely
    # gives back 40% of an intraday mark peak as spot oscillates, so a
    # 60%-of-peak floor converts a theta strategy into a scalp that pays
    # four legs of fees each time. Spreads therefore arm later and keep
    # more; directional trades keep the original aggressive settings.
    # `label` doubles as the risk class when it is one; every other
    # value (a symbol name, None) means "use the global settings".
    # An earlier version also read a `_rpf_class` config key that was
    # never set anywhere — dead code — and resolved the class with an
    # expression whose correctness depended on operator precedence
    # (`a or b if c else a`). Both removed; behaviour is unchanged and
    # asserted per-label by test.
    kls = label if label in ("spread", "futures", "option") else None
    def _p(name, default):
        if kls:
            v = cfg.get(f"rupee_profit_floor_{name}_{kls}")
            if v is not None:
                return v
        return cfg.get(f"rupee_profit_floor_{name}", default)
    arm = _p("arm_rupees", 750)
    keep = _p("keep_pct", 60) / 100.0
    min_worth = _p("min_rupees", 300)

    peak = max(float(state.get("rpf_peak", 0.0)), float(pnl_rupees))
    state["rpf_peak"] = peak
    if peak >= arm:
        candidate = round(peak * keep, 0)
        if candidate > float(state.get("rpf_floor", 0.0)):
            state["rpf_floor"] = candidate
    floor = float(state.get("rpf_floor", 0.0))
    if floor >= min_worth and pnl_rupees <= floor:
        # 2026-08-01 — report the P&L ACTUALLY REALISED, not the floor.
        # The old text ("gave back to ₹2310 of peak ₹4200") quoted the
        # protected level on trades that booked -₹3,000, because the
        # check only requires pnl <= floor and says nothing about how
        # far below it landed. A message that cannot report a failure
        # is not a report — it read as "the floor worked" on the four
        # exits where it most conspicuously had not.
        shortfall = ("" if pnl_rupees >= floor - 1 else
                     f" — MISSED the floor by ₹{floor - pnl_rupees:,.0f}")
        return (f"profit floor: exited at ₹{pnl_rupees:,.0f} against a "
                f"₹{floor:.0f} floor of peak ₹{peak:.0f} "
                f"(keeping {keep * 100:.0f}%"
                + (f", {kls} settings" if kls else "") + ")" + shortfall)
    return None


def _log_ai_advisory(agent, sym, kind, verdict, confidence, threshold, cfg, pnl,
                     trigger=None, latency=None):
    """Record EVERY AI exit advisory, not only the ones that act.

    2026-07-29: across 1,079 log lines and 31 closed trades, the AI
    exit advisory produced ZERO output. All three `*_ai_auto_exit_
    enabled` flags default False, and the advisory only ever surfaced
    itself via bus.alert() when the verdict was EXIT *and* confidence
    cleared the threshold. Every HOLD, and every sub-threshold EXIT,
    was computed and silently discarded.

    That makes the advisory impossible to evaluate: there is no way to
    ask "would enabling auto-exit have helped today?" because there is
    no record of what it said. This logs the verdict unconditionally,
    tagged with whether it WOULD have acted, so a session of shadow
    data can answer that question before anyone flips the switch.

    Cheap: capped at one line per position per 5 minutes by the
    existing `ai_ts` cadence guard in each caller.
    """
    if not cfg.get("ai_exit_advisory_logging", True):
        return
    advice = str(verdict.get("advice", "?"))
    would_act = advice == "EXIT" and confidence >= threshold
    enabled = cfg.get(f"{kind}_ai_auto_exit_enabled", False)
    if kind == "option":
        enabled = cfg.get("option_ai_auto_exit_enabled", False)
    state = ("WOULD EXIT (auto-exit ON)" if would_act and enabled else
             "WOULD EXIT (auto-exit OFF — advisory only)" if would_act else
             f"below threshold {threshold}%" if advice == "EXIT" else "hold")
    # Latency is logged because it decides whether ANY of this is
    # usable: ollama_timeout is 60s, and a verdict that arrives 40s
    # after the trigger describes a market that has already moved.
    # If these lines routinely show 20s+, the advisory cannot be an
    # exit input at all and should stay advisory-only.
    extra = ""
    if trigger:
        extra += f" · trigger: {trigger}"
    if latency is not None:
        extra += f" · took {latency:.1f}s"
    agent.bus.log(agent.name,
                  f"AI exit advisory · {sym} {kind}: {advice} {confidence}% "
                  f"[{state}] · P&L ₹{pnl:.0f} · {verdict.get('why', '')}{extra}")


def ai_exit_contradicts_position(leg, market_dir):
    """True when an AI EXIT verdict cites a move that FAVOURS the trade.

    2026-08-06. Every AI auto-exit this system has ever taken — five,
    across three sessions and both directions — closed a position
    because of a trend that helped it:

        08-04 NIFTY    PE  hold   4s  +73   "Market trending down..."
        08-04 NIFTY    PE  hold  23s  -229  "Market trending down..."
        08-05 NIFTY    PE  hold 100s  +171  "Market trending down..."
        08-06 SENSEX   CE  hold 828s   -68  "Market trending up..."
        08-06 FINNIFTY CE  hold   1s   -60  "Market trending up..."

    Every PE closed because the market was falling — which is what a put
    wants. Every CE closed because it was rising. Five for five.

    The prompt never stated the relationship, so the model treated any
    trend as a reason to exit. The prompt now says it AND returns
    `market_dir` as a field, so this check is structural rather than a
    regex over prose — a regex would have to guess at negation,
    hedging and phrasing the model has never been constrained to.

    Advisory alerts are unaffected: this only blocks the AUTOMATIC
    exit. A human reading "market trending up, consider exiting your
    call" can weigh it; an automatic exit acting on it cannot.
    """
    d = str(market_dir or "").strip().upper()
    if d not in ("UP", "DOWN"):
        return False                      # FLAT/absent -> no opinion
    return (leg == "CE" and d == "UP") or (leg == "PE" and d == "DOWN")


def ai_advisory_due(state, cfg, pnl, risk_rupees=None, near_stop=False):
    """Should an AI exit advisory run for this position RIGHT NOW?

    2026-07-29, in response to "after every 5 mins is too late": it is.
    Positions peaked and gave the gain back inside a single 5-minute
    window all session. But simply lowering the interval breaks two
    hard constraints at once:

      BUDGET.  ai_daily_call_cap is 400. A 375-minute session at a flat
               300s cadence across 4 positions already costs ~300 calls.
               At 60s it is ~1,500; with max_concurrent_spreads = 10 it
               is ~3,750. A blind cadence drop silently exhausts the cap
               mid-morning and then the advisory is dead for the rest of
               the day — worse than slow.
      LATENCY. ollama_timeout is 60s. The model itself can take tens of
               seconds, so at ANY fixed cadence the verdict describes a
               market state that has already moved on. This is why the
               LLM must not be the fast exit path; the deterministic
               reflexes (rupee profit floor, stop, target, defense zone)
               are, and they run on the 2s execution cycle.

    So the cadence becomes EVENT-DRIVEN rather than clock-driven. A
    quiet position is reviewed rarely; a position that has actually
    moved is reviewed within ~20s. On a calm day this uses FEWER calls
    than the flat 300s it replaces, and on a violent one it reacts in
    a fifth of the time.

    Returns a short trigger reason (also logged, so the trigger mix can
    be audited afterwards) or None.
    """
    now = time.time()
    since = now - state.get("ai_ts", 0)
    danger_iv = cfg.get("ai_exit_advisory_danger_interval_sec", 20)
    min_iv = cfg.get("ai_exit_advisory_min_interval_sec", 45)
    max_iv = cfg.get("ai_exit_advisory_max_interval_sec", 300)

    # Hard floor. Even a position in freefall cannot call more often
    # than this, or one bad minute drains the daily cap.
    if since < danger_iv:
        return None

    peak = max(float(state.get("ai_peak_pnl", 0.0)), float(pnl))
    state["ai_peak_pnl"] = peak
    last = state.get("ai_last_pnl")
    moved = abs(pnl - last) if last is not None else 0.0

    # 1) Danger — near the stop, or handing back a peak. Fastest path.
    giveback = (peak - pnl) / peak if peak > 0 else 0.0
    gb_trigger = cfg.get("ai_exit_advisory_giveback_trigger_pct", 30) / 100.0
    if (near_stop or (peak > 0 and giveback >= gb_trigger)) and since >= danger_iv:
        return (f"danger ({'near stop' if near_stop else f'gave back {giveback*100:.0f}% of peak'})")

    # 2) Material move, measured against the position's OWN risk so the
    #    same rule works for a ₹120 option and a 57,320 futures entry —
    #    the unit mistake that made every percentage-of-price threshold
    #    useless in the first place.
    if risk_rupees and risk_rupees > 0 and since >= min_iv:
        frac = moved / risk_rupees
        if frac >= cfg.get("ai_exit_advisory_move_trigger_pct", 25) / 100.0:
            return f"moved ₹{moved:.0f} ({frac*100:.0f}% of risk)"

    # 3) Periodic review so a genuinely quiet position is not ignored.
    if since >= max_iv:
        return "periodic review"
    return None


def trade_risk_fields(t):
    """Entry / stop / target / quantity for ANY closed trade, whichever
    schema it was written in.

    2026-08-01 — the journal holds two shapes. Options and spreads use
    `stoploss` / `target1` / `qty`; futures positions are persisted as
    the live position dict, so the same facts are `initial_sl` / `sl` /
    `target` / `lots` x `lot_size`. Both are complete — but a reader
    that knows only one shape sees None and concludes the data is
    missing.

    That is not hypothetical: the 2026-08-01 forensic analysis read
    `stoploss`, found None on all 19 futures trades, and reported that
    futures records "cannot reconstruct risk". They always could. The
    replay then fell back to scraping stop prices out of the exit-reason
    TEXT, which only worked for the 3 trades whose reason happened to
    contain one — so a 30-trade result was reported from 3.

    `initial_sl` is preferred over `sl` deliberately: `sl` may have been
    ratcheted by the trail or the profit floor before exit, so it
    answers "where was the stop at the end", while sizing was decided
    against the stop at ENTRY.

    Returns {entry, stop, target, qty, lots, side} with None for
    anything genuinely absent.
    """
    kind = trade_class(t)
    entry = t.get("entry")
    if kind == "futures":
        lots = t.get("lots")
        lot_size = t.get("lot_size")
        qty = (lots * lot_size) if (lots and lot_size) else t.get("qty")
        stop = t.get("initial_sl") or t.get("sl") or t.get("stoploss")
        target = t.get("target") or t.get("target1")
    else:
        qty = t.get("qty")
        lots = t.get("lots")
        # `initial_sl` FIRST, same rule the futures branch already used:
        # the option trail and the spread defense zone both mutate the
        # live stop, so `stoploss` on a closed record answers "where was
        # the stop at exit", while sizing was decided against entry.
        stop = t.get("initial_sl") or t.get("stoploss") or t.get("sl")
        target = t.get("target1") or t.get("target")
        # LEGACY SPREAD ROWS. Until 2026-08-02, exit_spread() wrote
        # `stoploss = -loss_limit` and `target1 = +profit_target` — both
        # P&L-per-share quantities in fields that mean PRICE. Those rows
        # are history and cannot be rewritten, so convert on READ into
        # the same spread-value basis the writer now uses:
        #     value at stop   = credit + loss_limit   = entry - stop
        #     value at target = credit - profit_target = entry - target
        # Detected by the sign, which is unambiguous: a negative stop
        # price cannot occur in the current shape, and a spread's target
        # is always BELOW its credit. Guarded on kind so a genuine option
        # buy is never touched.
        if kind == "spread" and t.get("stop_basis") is None:
            if stop is not None and stop < 0 and entry is not None:
                stop = round(entry + abs(stop), 2)
            if target is not None and entry is not None and target > 0 \
                    and target < entry:
                target = round(entry - target, 2)
    side = t.get("side")
    if not side and entry is not None and t.get("ltp") is not None:
        # options/spreads do not store a side; infer from the P&L sign
        pnl = float(t.get("pnl") or 0)
        side = "LONG" if (float(t["ltp"]) > float(entry)) == (pnl > 0) else "SHORT"
    return {"entry": entry, "stop": stop, "target": target, "qty": qty,
            "lots": lots, "side": side, "kind": kind}


def trade_class(t):
    """Which risk class a trade belongs to: spread | futures | option.

    One definition, used by the budget check, the profit floor and the
    Quality page, so the three cannot disagree about what a trade is.
    """
    s = str(t.get("strategy") or "")
    if "spread" in s or t.get("leg") == "SPREAD":
        return "spread"
    if t.get("kind") == "future" or float(t.get("entry") or 0) > 5000:
        return "futures"
    return "option"


def class_budget_blocked(cfg, closed_today, klass):
    """Per-class daily loss budget.

    2026-07-29 — spreads, single-leg buys and futures shared ONE
    `daily_loss_limit` and one kill-switch, so the worst-performing
    class could consume the whole day's risk and shut down the two that
    were working. It did: futures lost ₹23,863 over 40 trades while
    spreads made ₹15,235 and option buys ₹4,657. A single shared budget
    lets the loser spend the winners' allowance.

    Sub-budgets deliberately sum to MORE than the global
    `daily_loss_limit`. The global stays the hard ceiling; these only
    stop any ONE class from consuming all of it. Sizing them to sum
    exactly would make the last class to trade unable to trade at all.
    """
    if not cfg.get("risk_budgets_enabled", True):
        return None
    cap = cfg.get(f"budget_{klass}_daily_loss", 0)
    if not cap or cap <= 0:
        return None
    lost = -sum(float(t.get("pnl") or 0) for t in closed_today
                if trade_class(t) == klass and float(t.get("pnl") or 0) < 0)
    won = sum(float(t.get("pnl") or 0) for t in closed_today
              if trade_class(t) == klass and float(t.get("pnl") or 0) > 0)
    net_loss = lost - won
    if net_loss >= cap:
        return (f"{klass} daily budget spent: net -₹{net_loss:,.0f} "
                f"of ₹{cap:,.0f} (other classes unaffected)")
    return None


def dynamic_exit_reason(p, bus, cfg):
    """Evaluate a LIVE index-level condition against an open position.

    v58.47 — the mechanism pa_strategies' own note said did not exist:
    "there is no existing mechanism for 'keep evaluating an index-level
    condition on every future candle for an open position'." Every
    other PA strategy expresses its exit as fixed stop/target PRICES
    computed once at signal time, which the monitoring loop compares
    against. A histogram-turn exit cannot be expressed that way — it is
    a condition on FUTURE candles, not a level.

    Deliberately generic: `p["dynamic_exit"]` names a condition, so a
    second strategy can reuse this without touching the monitor loop.
    Only momentum_confluence sets it today.

    Returns a reason string or None. A missing candle pack returns None
    (skip, never force an exit on absent data — the standing rule).
    """
    kind = p.get("dynamic_exit")
    if not kind or not cfg.get("dynamic_exits_enabled", True):
        return None
    pack = bus.get(f"pa_candles:{p.get('symbol')}") or {}
    c1 = pack.get("c1")
    if not c1:
        return None
    d = 1 if p.get("leg") == "ce" or p.get("signal") == "BUY_CE" else -1
    if kind == "macd_hist_turn":
        import pa_strategies as _pa
        return _pa.macd_hist_turn_exit(c1, d)
    return None


def warn_zero_fees(bus, agent_name, kind, lots, fees):
    """Warn once per day when a trade closes booking ZERO cost.

    2026-07-29 — traced from a live journal. Eleven spreads closed that
    day: ten booked ₹0 fees, one booked ₹600. `lots` was correctly
    populated on all eleven (1..10) and the fee formula is a single
    line, so the only explanation is that `fee_per_lot` itself was 0
    for most of the session and 30 around 12:04 — a saved-setting
    change, not a code fault. NOTHING in the codebase writes that key.

    The defect is that it was SILENT. Zero-cost trades overstate every
    P&L figure on the P&L page, in the Quality breakdown, and in the
    backtests that `is_live_enabled()` reads to decide whether a
    strategy may trade real money. v58.41 added a warning, but only in
    BacktestAgent — which does not run when a trade closes.

    Once per day, not per trade: a repeated warning on every close is
    noise that gets filtered out, which is how the original went
    unnoticed for a full session.
    """
    if fees or not lots:
        return
    today = now_ist().strftime("%Y-%m-%d")
    if getattr(warn_zero_fees, "_day", None) == today:
        return
    warn_zero_fees._day = today
    msg = (f"{kind} closed with ZERO fees despite {lots} lot(s) — "
           f"fee_per_lot is 0. Every P&L figure today is overstated, "
           f"including the backtest numbers is_live_enabled() reads. "
           f"Set fee_per_lot in Settings.")
    bus.log(agent_name, msg)
    try:
        bus.alert("high", agent_name, "", "Trades booking zero cost")
    except Exception:
        pass


def should_log_throttled(agent, attr, key, reason, window=600):
    """Log-once-per-changed-reason, else at most every `window` seconds.

    2026-07-31 — extracted from ExecutionAgent._should_log_entry_fail so
    a second caller (the futures-OI archive) throttles by the SAME rule
    rather than growing a near-copy that drifts. State lives in a dict
    on the agent under `attr`, keyed by `key`.

    A changed reason always logs immediately: the point is to surface
    something NEW, not to rate-limit information away.
    """
    last = getattr(agent, attr, {})
    prev_reason, prev_ts = last.get(key, (None, 0))
    should_log = reason != prev_reason or time.time() - prev_ts > window
    if should_log:
        last[key] = (reason, time.time())
        setattr(agent, attr, last)
    return should_log


def data_age_of(bus, *keys, label="data"):
    """Age of a bus timestamp, distinguishing MISSING from STALE.

    2026-07-29, from a live log:

        skipping spread evaluation — analysis is 1785296955s old (> 90s)

    1,785,296,955 seconds is 56 years. The computation was
    `time.time() - (bus.get(key) or 0)`, so an ABSENT timestamp
    subtracted from zero and produced the current epoch. It failed
    safe — evaluation was skipped either way — but it conflated two
    different conditions that need different responses:

      MISSING  the feed has not delivered yet (normal at startup, and
               it resolves on its own)
      STALE    the feed delivered and then stopped (a real fault worth
               investigating)

    and a 56-year number reads like a clock bug, burying genuine
    staleness warnings in noise.

    Returns (age_seconds_or_None, human_reason).
    """
    ts = None
    for k in keys:
        ts = bus.get(k)
        if ts:
            break
    if not ts:
        return None, f"{label} not received yet (no timestamp on the bus)"
    age = time.time() - ts
    return age, f"{label} is {age:.0f}s old"


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
                self._consec_errors = 0
            except Exception as e:
                self.status = f"error: {e}"
                self.bus.log(self.name, f"⚠ {e}")
                # v59.71 (third-eye Tier 4) — an agent error used to be
                # one line in a 400-deep feed that rotates out in
                # minutes, and nothing anywhere inspected `status`. A
                # crashing cycle IS an outage of everything this agent
                # does. The first error and every 20th consecutive one
                # reach the alert stream, so "quiet because broken" can
                # no longer read as "quiet because idle".
                n = getattr(self, "_consec_errors", 0) + 1
                self._consec_errors = n
                # v59.72 (R2 finding L5) — TIME-throttled, not count-
                # throttled: every-20th on a 2s agent was ~2,160 HIGH
                # alerts/day for one permanently broken step. And the
                # alert call is guarded — an alerting failure must not
                # kill the agent thread it reports on.
                if n == 1 or time.time() - getattr(self, "_crash_alert_ts", 0) > 600:
                    self._crash_alert_ts = time.time()
                    try:
                        self.bus.alert("high", self.name, self.name.upper(),
                                       f"agent cycle CRASHED "
                                       f"({n} consecutive): "
                                       f"{type(e).__name__}: {e}")
                    except Exception:
                        pass
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

    @property
    def interval(self):
        # Dhan's option-chain endpoint hard-limits to 1 request/3s.
        # Kotak's documented limit is 10 requests/second across ALL
        # APIs (confirmed in their official docs) — a full chain fetch
        # needs ~3-5 sequential calls (index quote + option batches,
        # occasionally +OI), each already spaced by the global rate
        # limiter in broker_adapter.py, so cycling faster here is safe
        # and meaningfully cuts the observed refresh lag.
        try:
            broker = config.load().get("broker", "dhan")
        except Exception:
            broker = "dhan"
        return 1.0 if broker == "kotak" else 3

    def cycle(self):
        syms = self.bus.get("symbols", ["NIFTY"])
        active = self.bus.get("active_symbol") or syms[0]
        # CRITICAL: a symbol with an open position must refresh every
        # cycle — never fall back to slow background rotation just because
        # the user is looking at a different tab. Stale price data on an
        # open trade is how profit turns into a missed stoploss.
        positions = self.bus.get("positions", {}) or {}
        # v59.70 (third-eye Tier 3, round 2) — "open trades first" only
        # counted single-leg option positions. A symbol whose only open
        # exposure was a SPREAD or a FUTURES contract fell back to the
        # slow background rotation, so exactly the trades the spread/
        # futures monitors were watching got the stalest data. All three
        # books count as open now.
        open_syms = list(dict.fromkeys(
            list(positions.keys())
            + [sp.get("symbol") for sp in
               (self.bus.get("spreads", {}) or {}).values() if sp.get("symbol")]
            + list((self.bus.get("futures_positions", {}) or {}).keys())))
        i = self.bus.get("_md_idx", 0)
        self.bus.set("_md_idx", i + 1)
        if open_syms:
            # cycle through open positions first (they need the freshest
            # data); only touch other symbols on the odd slot if there's
            # exactly one open position (spare bandwidth for the display)
            sym = open_syms[i % len(open_syms)]
            if len(open_syms) == 1 and i % 3 == 2:
                others = [s for s in syms if s not in open_syms] or syms
                sym = others[(i // 3) % len(others)] if active in open_syms else active
        elif i % 2 == 0 and active in syms:
            sym = active
        else:
            others = [s for s in syms if s != active] or syms
            sym = others[(i // 2) % len(others)]
        chain = None
        fail_until = self.bus.get(f"md_fail_until:{sym}", 0)
        if time.time() < fail_until:
            # this symbol is in a cooldown after repeated failures — skip
            # it silently this cycle rather than hammering a broken call
            self.summary = f"{sym} backing off (see earlier error) · cycling {len(syms)} indices"
            return
        # 2026-07-28 — real gap found, per explicit suggestion after
        # investigating the 429 rate-limit escalation: this cycle had
        # NO market-hours gate at all — it fetched option chains (and,
        # via _poll_futures_via_rest below, futures quotes) 24/7,
        # including all evening/overnight/weekend hours, explaining why
        # 429 hits were spread fairly evenly across every hour of the
        # day rather than concentrated in the ~6.25 trading hours.
        # Safe to skip entirely outside market hours: chain:{sym} (and
        # everything downstream of it) simply keeps its last value,
        # which is exactly the existing "show the last available
        # session" design already relied on elsewhere (RegimeAgent's
        # own stale-session fallback, the chart's most-recent-session
        # tiers, etc.) — this doesn't change what gets DISPLAYED, it
        # just stops re-fetching data nobody asked to refresh.
        if not fno_session_open():
            self.summary = f"market closed — not fetching (last data retained) · cycling {len(syms)} indices"
            return
        try:
            chain = self.ctx["get_chain"](sym)
        except Exception as e:
            fails = self.bus.get(f"md_fails:{sym}", 0) + 1
            self.bus.set(f"md_fails:{sym}", fails)
            if fails <= 1 or fails % 20 == 0:
                self.bus.log(self.name, f"{sym}: {e}")
            if "429" in str(e) or "Too Many Requests" in str(e):
                # explicit rate-limit signal from the broker — back off
                # hard rather than the smaller generic backoff, and don't
                # let it grow unbounded (60s is already a long wait for a
                # single symbol's refresh)
                backoff = 60
            else:
                backoff = min(300, 10 * (2 ** min(fails, 5)))   # 20s.. capped at 5min
            self.bus.set(f"md_fail_until:{sym}", time.time() + backoff)
            self.summary = f"{sym} fetch failed ({fails}x) — backing off {backoff}s"
            return
        self.bus.set(f"md_fails:{sym}", 0)
        self.bus.set(f"chain:{sym}", chain)
        self.bus.set(f"chain_ts:{sym}", time.time())
        self.bus.set("chain_ts", time.time())
        self._sync_ws_feed(sym, chain)
        # 2026-08-03 — futures data was dead for a full session and
        # nothing said so. `_sync_ws_feed` returns early whenever the
        # websocket can't start (REST mode, or `dhanhq` missing from the
        # interpreter the app happens to be running under), and it was
        # the ONLY caller of _ensure_futures_subscribed — so
        # _future_sec_ids stayed empty and the REST poller below returned
        # instantly on an empty map. No error, no log: the futures OI
        # archive simply wrote nothing, which is indistinguishable from
        # "this account has no futures". Resolved unconditionally here so
        # REST is a real fallback rather than one that depends on the
        # websocket it is falling back FROM.
        self._ensure_futures_subscribed(None)
        self._poll_futures_via_rest()
        # spot history for intraday momentum (no extra API calls)
        hist = self.bus.get(f"spot_hist:{sym}", [])
        if chain.get("spot"):
            hist.append((time.time(), chain["spot"]))
            self.bus.set(f"spot_hist:{sym}", hist[-800:])
        # live ticker entry (prev_close filled by app-side cache, but
        # some brokers — Kotak — return change/% directly on the quote,
        # which is used as a fallback when candle-derived prev_close
        # isn't available for that broker)
        tick = self.bus.get("ticker", {})
        tick[sym] = {"spot": chain.get("spot"),
                     "ts": now_ist().strftime("%H:%M:%S"),
                     "chg": chain.get("chg"), "chg_pct": chain.get("chg_pct")}
        self.bus.set("ticker", tick)
        self.summary = f"{sym} {chain.get('spot')} · cycling {len(syms)} indices"

    # ---------------------------------------------------------- hybrid feed
    # HYBRID DESIGN (2026-07-24, validated live 2026-07-23 against a real
    # Dhan account — see ROADMAP.md and dhan_ws.py's module docstring for
    # the full design review of what was and wasn't built and why):
    #
    # REST (above) stays the ONLY source of chain SHAPE — which strikes
    # exist, IV, greeks — the websocket feed has none of that. What it
    # adds is faster, in-between-REST-poll freshness for the fields it
    # DOES carry: spot (index Ticker packets) and LTP/OI/depth (option
    # Full packets), merged onto the same REST-fetched chain dict via
    # dhan_ws.merge_tick_into_chain() rather than replacing any of it.
    # This is deliberately additive: if `market_data_feed` is anything
    # other than "websocket" (the default is "rest"), none of this runs
    # and MarketDataAgent behaves exactly as it always has.
    def _sync_ws_feed(self, sym, chain):
        cfg = config.load()
        if cfg.get("market_data_feed", "rest") != "websocket":
            return
        if dhan_ws is None or cfg.get("broker", "dhan") != "dhan":
            return
        client = self._ensure_ws_client(cfg)
        if client is None:
            return
        self._ensure_futures_subscribed(client)
        subscribed = getattr(self, "_ws_subscribed_legs", None)
        if subscribed is None:
            subscribed = set()
            self._ws_subscribed_legs = subscribed
        # As REST discovers new strikes over time (spot drifting to a new
        # ATM zone, expiry roll, etc.), grow the websocket subscription to
        # match — REST is still what decided these strikes exist at all.
        for row in chain.get("rows", []):
            for leg_key in ("ce", "pe"):
                leg = row.get(leg_key) or {}
                sec_id = leg.get("security_id")
                if not sec_id or sec_id in subscribed:
                    continue
                try:
                    if client.subscribe_more(sym, sec_id):
                        subscribed.add(sec_id)
                    # else: connection not up yet (or bad segment) —
                    # deliberately NOT added to `subscribed`, so the next
                    # cycle (this method runs every ~3s) retries it rather
                    # than silently losing it forever
                except Exception as e:
                    self.bus.log(self.name, f"ws subscribe_more failed for "
                                 f"{sym} {sec_id}: {e}")

    def _ensure_futures_subscribed(self, client=None):
        """Resolve (and, with a client, subscribe) the current-month
        future per symbol via the scrip master lookup, re-checked once
        per trading day.

        RESOLUTION AND SUBSCRIPTION ARE SEPARATE PASSES (2026-08-03).
        They used to be one: a contract entered `_future_sec_ids` only
        if `client.subscribe_more()` returned True. But `_future_sec_ids`
        is also what `_poll_futures_via_rest` reads, and the only caller
        of this method was `_sync_ws_feed`, which returns early whenever
        the websocket can't start. So on 2026-08-03 — `dhanhq` missing
        from the interpreter the app was launched with — the websocket
        never started, no contract was ever registered, the REST poller
        returned instantly on an empty map, and futures LTP/OI produced
        NOTHING for a full session while every log line looked normal.
        `future_oi_snapshots` has exactly one day in it for this reason.
        The REST fallback depended on the websocket it falls back FROM.

        Now resolution runs off the scrip master alone and is called
        unconditionally from the cycle; subscription is a second pass
        that needs a client and retries until it sticks. `client=None`
        is the REST-mode call and is not an error.

        Re-checking
        daily (rather than once ever) is what makes monthly rollover
        automatic: dhan_scrip_master.get_current_futures_detailed()
        always resolves to whichever contracts are nearest-unexpired —
        if the front one is a different security_id than yesterday's
        (the old one expired, a new month is now current), this picks
        it up with no code change or manual security-ID update needed.

        Extended 2026-07-25 per explicit request ("there are 2 more
        months - capture those as well"): now subscribes up to 3
        nearest-expiry contracts per symbol, not just the front month.
        The FRONT month keeps its exact existing role unchanged — it's
        still the only one driving future_oi_trend:{sym}/future_ohlc:
        {sym} (the live OI-buildup strategy signal and LTP Monitor
        panel), so nothing about the existing strategy pipeline
        changes. The 2nd/3rd months are additive: tracked separately
        under future_months:{sym} for cross-month OI/volume-wall
        analysis — captured now so that data has a lead time before
        any UI/strategy actually consumes it, same pattern already
        used for the candle-DB and volume-profile persistence work.

        Reuses subscribe_more() (the same method used for option legs)
        rather than add_future_instrument() — that method is designed
        for the pre-connection instrument list passed to start(), not
        for adding to an already-open connection. Futures use the same
        NSE_FNO/BSE_FNO Full-mode subscription shape as option legs, so
        subscribe_more() applies identically once the security_id is
        resolved here.
        """
        if dhan_scrip_master is None:
            return
        today = now_ist().strftime("%Y-%m-%d")
        checked = getattr(self, "_futures_checked_date", None)
        if checked is None:
            checked = {}
            self._futures_checked_date = checked
        future_map = getattr(self, "_future_sec_ids", None)
        if future_map is None:
            future_map = {}
            self._future_sec_ids = future_map
        # sec_id -> "front"/"month2"/"month3", so _on_ws_tick/_classify_
        # future_tick can tell the strategy-driving front-month contract
        # apart from the additive far-month ones without changing the
        # front-month code path at all.
        future_roles = getattr(self, "_future_roles", None)
        if future_roles is None:
            future_roles = {}
            self._future_roles = future_roles
        # 2026-07-25 — fixed a real staggered-delay bug: this used to
        # call get_current_futures_detailed() once PER symbol, each of
        # which independently parsed+scanned the full ~200k-row scrip
        # master CSV from scratch — a live report showed this as a
        # visible ~3-5s-per-symbol delay, one index after another, at
        # startup (or on any day a new month's contract needs
        # resolving). get_current_futures_for_symbols() does the
        # expensive parse ONCE for all requested symbols together.
        contracts = getattr(self, "_future_contracts", None)
        if contracts is None:
            contracts = {}
            self._future_contracts = contracts
        ws_subscribed = getattr(self, "_future_ws_subscribed", None)
        if ws_subscribed is None:
            ws_subscribed = set()
            self._future_ws_subscribed = ws_subscribed
        pending = [sym for sym in self.bus.get("symbols", [])
                  if checked.get(sym) != today]
        results_by_symbol = None
        if pending:
            try:
                results_by_symbol = dhan_scrip_master.get_current_futures_for_symbols(
                    pending, n=3)
            except Exception as e:
                for sym in pending:
                    self.bus.log(self.name, f"futures lookup failed for {sym}: {e}")
                    checked[sym] = today   # don't hammer this every 3s cycle today
        for sym in (pending if results_by_symbol is not None else []):
            futures, detail = results_by_symbol.get(sym, ([], {}))
            if not futures:
                self.bus.log(self.name, f"no current future found for {sym}: {detail}")
                checked[sym] = today
                continue
            newly_resolved = []
            for i, future in enumerate(futures):
                role = "front" if i == 0 else f"month{i + 1}"
                sec_id = int(future["security_id"])
                if sec_id in future_map:
                    continue   # already resolved this exact contract
                # REGISTERED ON RESOLUTION, NOT ON SUBSCRIPTION. See the
                # docstring: _poll_futures_via_rest reads these two maps,
                # so gating them on subscribe_more() made the REST
                # fallback depend on the websocket it falls back FROM.
                future_map[sec_id] = sym
                future_roles[sec_id] = role
                contracts[sec_id] = {
                    "sym": sym, "raw": future["security_id"], "role": role,
                    "name": future.get("symbol_name", "?"),
                    "expiry": future["expiry"]}
                newly_resolved.append((role, future))
            checked[sym] = today
            for role, future in newly_resolved:
                self.bus.log(self.name,
                            f"{sym} {role} future resolved: "
                            f"{future.get('symbol_name', '?')} "
                            f"(security_id={future['security_id']}, "
                            f"expiry={future['expiry'].date()})")
                if role == "front":
                    # 2026-07-26 (v52) — published so enter_future() can
                    # read it directly. The prior Phase-1 code tried
                    # `(bus.get(future_months:{sym}) or [{}])[0]` on what
                    # is actually a DICT keyed by role, not a list —
                    # [0] on a real dict would KeyError the instant this
                    # path was exercised (never hit in Phase 1 testing
                    # since expiry wasn't asserted on). expiry also was
                    # never stored on future_months entries in the first
                    # place (_future_month_tick only keeps security_id/
                    # ltp/oi/volume) — a separate, dedicated key avoids
                    # both problems.
                    self.bus.set(f"future_expiry:{sym}",
                                 future["expiry"].strftime("%Y-%m-%d"))

        # ------------------------------------------------ subscription pass
        # Separate from resolution above and retried every cycle until it
        # sticks, so a websocket that connects late — or reconnects, or
        # only becomes importable after a restart — still picks up
        # contracts that were resolved without it. `checked` deliberately
        # no longer gates this: it stamps RESOLUTION, which is a
        # once-a-day scrip-master lookup, not subscription, which is a
        # connection state that can change any time.
        if client is None:
            return
        for sec_id, c in list(contracts.items()):
            if sec_id in ws_subscribed:
                continue
            try:
                ok = client.subscribe_more(c["sym"], c["raw"])
            except Exception as e:
                if should_log_throttled(self, "_fut_sub_fail", str(sec_id),
                                        f"{type(e).__name__}: {e}"):
                    self.bus.log(self.name,
                                 f"⚠ {c['sym']} {c['role']} future subscribe "
                                 f"failed ({type(e).__name__}: {e}) — REST "
                                 f"polling still covers LTP/OI")
                continue
            if ok:
                ws_subscribed.add(sec_id)
                self.bus.log(self.name,
                            f"{c['sym']} {c['role']} future subscribed: "
                            f"{c['name']} (security_id={c['raw']}, "
                            f"expiry={c['expiry'].date()})")

    def _future_month_tick(self, sym, sec_id, tick):
        """Lightweight LTP/OI tracker for the 2nd/3rd month contracts
        (role != "front") — same tick data _classify_future_tick uses
        for the front month, but WITHOUT the buildup-classification/
        strategy-signal machinery, since that's specified against the
        front month only. Published to future_months:{sym} keyed by
        role, for cross-month OI/volume-wall analysis to consume later
        — data captured now, analysis not yet built (same "capture
        first, analyze later" pattern as the candle DB and volume-
        profile persistence work)."""
        ltp, oi = tick.get("ltp"), tick.get("oi")
        if ltp is None:
            return
        role = self._future_roles.get(sec_id, "?")
        months = getattr(self, "_future_months", None)
        if months is None:
            months = {}
            self._future_months = months
        sym_months = months.setdefault(sym, {})
        sym_months[role] = {"security_id": sec_id, "ltp": ltp, "oi": oi,
                            "volume": tick.get("volume")}
        self.bus.set(f"future_months:{sym}", sym_months)

    def _poll_futures_via_rest(self):
        """REST-based supplement for futures LTP/OI, using Dhan's
        Market Quote batch endpoint (up to 1000 instruments, 1 req/
        SECOND — a completely separate, much faster rate limit than
        the option-chain endpoint's ~3.2s gap). Added 2026-07-25 per
        explicit live report: SENSEX futures data wasn't updating via
        the websocket-only path. Root cause is plausibly NOT a
        subscription bug at all — a websocket tick only arrives when a
        genuine trade actually prints, and a less-liquid contract like
        SENSEX's far-month futures can go long stretches with nothing
        to show even with a perfectly correct subscription. This REST
        poll guarantees a fresh read every ~3s cycle regardless of how
        often the contract actually trades, independent of whatever
        the websocket subscription is or isn't receiving.

        Reuses the EXISTING _classify_future_tick()/_future_month_tick()
        methods by building a synthetic tick dict from the REST
        response — no duplicate buildup-classification/OHLC-tracking
        logic, same downstream bus keys (future_oi_trend/future_ohlc/
        future_months) either way, so nothing consuming those keys
        needs to know or care which transport the data arrived over."""
        future_roles = getattr(self, "_future_roles", None)
        future_sec_ids = getattr(self, "_future_sec_ids", None)
        if not future_roles or not future_sec_ids:
            # 2026-08-03 — this bare `return` hid a whole dead session.
            # An empty map means NO futures data at all: no LTP, no OI,
            # no future_oi_snapshots rows, and therefore no S10 futures
            # backtest — and it looked exactly like a quiet market. Say
            # so, throttled, because it is a real outage of a data feed
            # and silence is what made it cost a session to notice.
            if should_log_throttled(self, "_fut_unresolved", "all",
                                    "no futures resolved"):
                self.bus.log(self.name,
                             "⚠ no futures contracts resolved — futures "
                             "LTP/OI unavailable and the OI archive is "
                             "writing nothing (S10 backtests stay "
                             "mode=chain_only)")
            return
        # 2026-07-25 — real bug found from a live log: repeated "429 Too
        # Many Requests" on this endpoint for ~9 minutes straight with
        # no recovery. The 1.2s pacing below is fine in isolation, but
        # there was no backoff after an actual 429 — it just retried
        # again 1.2s later and hit the same wall, over and over,
        # self-sustaining the rate-limit lockout instead of easing off
        # it. Fixed with the same fail_until cooldown pattern already
        # used for the option-chain REST fetch's own 429 handling in
        # this same agent's cycle().
        fail_until = getattr(self, "_quote_batch_fail_until", 0)
        if time.time() < fail_until:
            return
        last = getattr(self, "_last_quote_batch_call", 0)
        # 2026-07-28 — real gap found from a live log showing this
        # endpoint's rate-limit hits climbing sharply day over day
        # (28 on 07-16, 61 on 07-25, 155 on 07-26, 286 on 07-27, 1,151
        # on 07-28) despite the 2.5s pacing already in place from the
        # PRIOR fix (see the 2026-07-26 note below, kept for history).
        # Two changes, not one, since either alone leaves a real gap:
        #   1. Gap widened 2.5s -> 4s (~0.25 req/s). Still comfortably
        #      inside Dhan's documented 1 req/s ceiling for this
        #      endpoint with more headroom against jitter — but this
        #      caller's own pacing was ALREADY inside the ceiling at
        #      2.5s, and errors kept climbing anyway, which is the real
        #      tell: something ELSE is very plausibly sharing whatever
        #      the true rate budget is (the option-chain endpoint is
        #      hit far more often, every ~3s per symbol — 4x this
        #      caller's own traffic — and if Dhan enforces this at the
        #      account level rather than strictly per-endpoint, that
        #      traffic alone could already be consuming most of the
        #      budget). Not able to confirm Dhan's exact policy from
        #      this environment, so widening the gap is the safe,
        #      concrete half of the fix rather than a guess dressed up
        #      as a diagnosis.
        #   2. The escalating backoff (60s -> 5min) was already
        #      resetting to zero on the very FIRST successful call
        #      after a failure streak — so one lucky success in a
        #      contended window would immediately drop the backoff
        #      back to a short retry, which could fail again right
        #      away. Now requires 3 consecutive successes before fully
        #      clearing the streak, so recovery is more deliberate and
        #      isn't undone by a single fortunate gap in the
        #      contention.
        #
        # 2026-07-26 — gap raised from 1.2s to 2.5s. Dhan documents
        # /marketfeed/quote at 1 request/SECOND, and this method is
        # invoked at the end of EVERY per-symbol chain fetch — i.e. up
        # to 4x per ~3s agent cycle. At a 1.2s gap that sustains ~0.83
        # req/s: technically under the ceiling, but with no headroom at
        # all, so any jitter or a retry trips 429. Confirmed there is
        # exactly one caller of quote_batch() in the codebase for THIS
        # specific pacing state, though see the 2026-07-28 note above
        # for why that alone doesn't rule out shared-budget contention
        # from other Dhan endpoints.
        if time.time() - last < 4.0:
            return
        self._last_quote_batch_call = time.time()
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None or dhan_ws is None:
            return
        seg_map = {}
        for sec_id, sym in future_sec_ids.items():
            segment = dhan_ws.SEGMENT_FOR_SYMBOL.get(sym)
            if segment:
                seg_map.setdefault(segment, []).append(sec_id)
        if not seg_map:
            return
        try:
            data = d.quote_batch(seg_map)
        except Exception as e:
            fails = getattr(self, "_futures_rest_fails", 0) + 1
            self._futures_rest_fails = fails
            is_429 = "429" in str(e) or "Too Many Requests" in str(e)
            if is_429:
                # 2026-07-26 — was a flat 60s every time, so a sustained
                # lockout produced an endless 60s-retry-fail loop (visible
                # in the live log as 429s every 2-3 minutes for hours
                # rather than easing off). Now escalates 60s -> 5min
                # while the failures keep coming, and resets (see the
                # 2026-07-28 note above) only after 3 consecutive
                # successes rather than immediately on the first one.
                consecutive = getattr(self, "_futures_429_streak", 0) + 1
                self._futures_429_streak = consecutive
                self._futures_success_streak = 0   # any failure restarts the recovery count
                backoff = min(300, 60 * consecutive)
            else:
                backoff = min(300, 10 * (2 ** min(fails, 5)))
            self._quote_batch_fail_until = time.time() + backoff
            if fails <= 1 or fails % 20 == 0:
                self.bus.log(self.name, f"futures REST quote poll failed: "
                             f"{e} — backing off {backoff}s")
            return
        self._futures_rest_fails = 0
        # 2026-07-28 — see the pacing block above: a single success no
        # longer immediately clears the streak. Requires 3 in a row so
        # a lucky gap in whatever contention caused the failures
        # doesn't undo the escalation prematurely, only to fail again
        # right away.
        successes = getattr(self, "_futures_success_streak", 0) + 1
        self._futures_success_streak = successes
        if successes >= 3:
            self._futures_429_streak = 0     # recovered — reset the escalation
        for rows in (data or {}).values():
            for sec_id_str, q in (rows or {}).items():
                try:
                    sec_id = int(sec_id_str)
                except (TypeError, ValueError):
                    continue
                sym = future_sec_ids.get(sec_id)
                if not sym:
                    continue
                tick = {"ltp": q.get("last_price"), "oi": q.get("oi"),
                       "volume": q.get("volume")}
                if tick["ltp"] is None:
                    continue
                if future_roles.get(sec_id, "front") == "front":
                    self._classify_future_tick(sym, tick)
                else:
                    self._future_month_tick(sym, sec_id, tick)

    def _classify_future_tick(self, sym, tick):
        """Classify long/short buildup from a futures LTP+OI tick,
        comparing against a baseline captured at the first tick of
        each trading day — buildup is a session-level concept (has
        today's positioning grown net long or net short), not
        tick-to-tick noise, so a same-day baseline is the right
        reference point rather than the previous tick.

        Writes exactly "long" / "short" / None to
        future_oi_trend:{symbol} — matching mtf_confluence_strategy.
        evaluate()'s future_buildup parameter precisely (confirmed by
        reading its exact comparisons before writing this, not
        assumed). Only the STRICT textbook buildup quadrants map to a
        signal:
          price up + OI up   -> "long"  (long buildup, per rinkoo.docx)
          price down + OI up -> "short" (short buildup, per rinkoo.docx)
        The other two quadrants (short covering: price up + OI down;
        long unwinding: price down + OI down) are real, commonly-
        watched signals too, but are a WEAKER/different read than the
        strict "buildup" the doc specifically asks for — reported only
        on the richer diagnostic key (future_oi_quadrant), not fed into
        the strategy's future_buildup input, so the strategy only ever
        sees the exact signal it was specified against.
        """
        ltp, oi = tick.get("ltp"), tick.get("oi")
        if ltp is None:
            return
        today = now_ist().strftime("%Y-%m-%d")
        # 2026-07-25 — real bug found while investigating "SENSEX futures
        # data not updating": this used to bail out entirely (before
        # even updating OHLC/LTP) whenever `oi` was missing/None on a
        # tick. But the LTP Monitor's Futures panel only needs LTP —
        # OI-buildup classification is a separate concern that genuinely
        # needs OI. Some BSE_FNO contracts' REST quote responses may not
        # populate `oi` reliably (lower liquidity, or a BSE-specific gap
        # in what Dhan returns) — that should degrade the OI-buildup
        # signal gracefully, not silently freeze the futures OHLC panel
        # too. Fixed: OHLC/LTP tracking now runs whenever LTP alone is
        # present; only the OI-buildup classification below still
        # requires OI specifically.
        self._update_future_ohlc(sym, ltp, today, tick.get("volume"), tick)
        if oi is None:
            return
        baselines = getattr(self, "_future_baseline", None)
        if baselines is None:
            baselines = {}
            self._future_baseline = baselines
        b = baselines.get(sym)
        if not b or b.get("date") != today:
            baselines[sym] = {"date": today, "ltp": ltp, "oi": oi}
            self.bus.set(f"future_oi_trend:{sym}", None)
            self.bus.set(f"future_oi_quadrant:{sym}", None)
            return
        price_up, oi_up = ltp > b["ltp"], oi > b["oi"]
        if price_up and oi_up:
            quadrant, trend = "long_buildup", "long"
        elif not price_up and oi_up:
            quadrant, trend = "short_buildup", "short"
        elif price_up and not oi_up:
            quadrant, trend = "short_covering", None
        else:
            quadrant, trend = "long_unwinding", None
        self.bus.set(f"future_oi_trend:{sym}", trend)
        self.bus.set(f"future_oi_quadrant:{sym}", quadrant)
        # v58.66 -- persist it. This was computed live and discarded, so
        # the FUTURES half of Strategy 10's trigger could not be
        # backtested at all while the option half had 5 days of 60s
        # snapshots. One row per symbol per cycle.
        # 2026-07-31 — this was `except Exception: pass`. On 31 July the
        # table did not exist at all, which could only mean the call had
        # never run; proving that took a probe against a scratch DB,
        # because a silent failure and a never-executed line look
        # identical from the outside. Same swallow that hid the S9 `mcs`
        # NameError (v58.47), the undefined `oi_chg`/`chg` here (v58.66)
        # and S10's own AttributeError (v58.68). A failure that cannot be
        # observed is indistinguishable from a feature that was never
        # wired, and both take a version or more to notice.
        #
        # Throttled by reason so a persistent fault (disk full, locked
        # DB) does not spam once per symbol per cycle, and announced ONCE
        # on first success so a live session gives positive evidence the
        # archive is running rather than silence that could mean either.
        try:
            import history as _h
            _h.log_future_oi(sym, time.time(), oi, oi - b["oi"],
                             ltp, ltp - b["ltp"], quadrant)
            # 2026-08-03 — was `if not getattr(self, "_foi_archive_ok",
            # False)`: announce once per PROCESS, then silence forever.
            # That is what let this archive die unnoticed. The line fired
            # on 31 July and never again, and since it can only fire once
            # per restart, its absence across every LATER restart read as
            # "still fine" rather than "has not run since". A success
            # signal whose absence is ambiguous is not a signal.
            #
            # Now once per DAY, through the shared throttle — the date IS
            # the reason, so a new day always logs. One line a session,
            # and a missing line unambiguously means it did not write.
            if should_log_throttled(self, "_foi_archive_daily", "all",
                                    now_ist().strftime("%Y-%m-%d"),
                                    window=86400):
                self.bus.log(self.name,
                             "futures OI archive active — S10 backtests can "
                             "run mode=full once a session is recorded")
        except Exception as e:
            if should_log_throttled(self, "_foi_archive_fail", sym,
                                    f"{type(e).__name__}: {e}"):
                self.bus.log(self.name,
                             f"⚠ {sym}: futures OI archive FAILED "
                             f"({type(e).__name__}: {e}) — S10 backtests "
                             f"stay mode=chain_only")
        self.bus.set(f"future_tick:{sym}",
                     {"ltp": ltp, "oi": oi, "baseline_ltp": b["ltp"],
                      "baseline_oi": b["oi"]})

    def _update_future_ohlc(self, sym, ltp, today, cum_volume=None, tick=None):
        """Session OHLC + a VWAP proxy for the future, extending the
        existing futures tick pipeline (LTP Monitor enhancement,
        feature #1) — reuses the exact same tick data
        _classify_future_tick already receives, no new subscription.

        VWAP proxy, not a true volume-weighted average: Dhan's Full
        packet exposes cumulative session volume, not a clean per-tick
        trade-size delta, so a mathematically correct VWAP would need
        extra reconstruction with real risk of getting it subtly wrong.
        This uses a running mean of LTP across ticks (a TWAP) instead —
        same honest tradeoff already documented and accepted elsewhere
        in this codebase for the spot side (AnchorPullback's "session
        anchor" is explicitly a TWAP proxy for the same reason: no
        clean per-trade volume signal available). Labeled "vwap" in the
        API response for trader-familiar terminology, but this
        docstring and the roadmap entry are explicit about what it
        actually is.

        2026-07-25 — per explicit instruction ("Volume is NOT
        optional"): `cum_volume` (the tick's own cumulative-session-
        volume field, already fetched every cycle for the futures
        quote — same tick dict _classify_future_tick already receives,
        no new API call) is now tracked and used to build a real
        PER-MINUTE volume series — the delta between this minute's
        final cumulative reading and the previous minute's, which is
        genuine traded volume during that minute (not just a repeated
        running total). Same bucket-on-minute-rollover technique
        MarketDataAgent._build_candle already uses for price candles,
        applied to volume instead of OHLC — deliberately reusing that
        established pattern rather than inventing a different one.
        Persisted via history.upsert_volume_history() so the chart's
        Volume pane has real historical data, not just the current
        session's running total.
        """
        ohlc = getattr(self, "_future_ohlc", None)
        if ohlc is None:
            ohlc = {}
            self._future_ohlc = ohlc
        o = ohlc.get(sym)
        # "ts" (v59.69) — the futures quote had NO timestamp at all, so
        # exit monitoring could not age-check it even in principle.
        if not o or o.get("date") != today:
            ohlc[sym] = {"date": today, "open": ltp, "high": ltp, "low": ltp,
                        "close": ltp, "vwap_sum": ltp, "vwap_n": 1,
                        "vwap": ltp, "volume": cum_volume, "ts": time.time()}
            self.bus.set(f"future_ohlc:{sym}", ohlc[sym])
        else:
            o["high"] = max(o["high"], ltp)
            o["low"] = min(o["low"], ltp)
            o["close"] = ltp
            o["vwap_sum"] += ltp
            o["vwap_n"] += 1
            o["vwap"] = round(o["vwap_sum"] / o["vwap_n"], 2)
            if cum_volume is not None:
                o["volume"] = cum_volume
            o["ts"] = time.time()
            self.bus.set(f"future_ohlc:{sym}", o)

        if cum_volume is not None:
            self._build_volume_candle(sym, cum_volume)
        self._record_basis_residual(sym, ltp)
        self._archive_futures_candle(sym, ltp, cum_volume, tick)

    def _archive_futures_candle(self, sym, ltp, cum_volume, tick=None):
        """Persist a real per-minute FUTURES OHLCV+OI candle.

        v59.0 item 4, and the gating dependency for any futures re-test.
        Phase A had to drive every strategy with INDEX candles because
        futures prices were never archived — only volume, and only for 5
        sessions. So no Phase A result ran on the instrument itself, and
        none can until this series exists.

        Starts today and builds forward; the first valid re-test window
        opens once enough sessions accumulate. Writes o/h/l/c/v/oi in ONE
        row via upsert_candles, rather than layering an OHLC write over
        upsert_volume_history's v-only row — a naive REPLACE there would
        null the volume the other writer had just stored.

        `session_only` stays default True: an out-of-hours futures bar is
        the same keepalive contamination the v58.71 write gate exists to
        refuse.
        """
        now = time.time()
        bucket = int(now // 60) * 60
        st = getattr(self, "_fut_candle_state", None)
        if st is None:
            st = {}
            self._fut_candle_state = st
        oi = None
        if isinstance(tick, dict):
            for k in ("oi", "openInterest", "open_interest", "OI"):
                if tick.get(k) is not None:
                    oi = tick.get(k)
                    break
        cur = st.get(sym)
        if cur is None or cur["bucket"] != bucket:
            if cur is not None:
                try:
                    import history as _h
                    n = _h.upsert_candles(f"{sym}_FUT_1m", [{
                        "ts": cur["bucket"], "o": cur["o"], "h": cur["h"],
                        "l": cur["l"], "c": cur["c"],
                        "v": (max(0, cur["last_cum"] - cur["start_cum"])
                              if cur["start_cum"] is not None
                              and cur["last_cum"] is not None else None),
                        "oi": cur["oi"]}])
                    # Same once-per-process latch as the OI archive above,
                    # and the same fix: per DAY, per symbol. Announcing a
                    # working archive exactly once and then never again
                    # means silence covers both "running fine" and "stopped
                    # three days ago".
                    if n and should_log_throttled(
                            self, "_fut_archive_daily", sym,
                            now_ist().strftime("%Y-%m-%d"), window=86400):
                        self.bus.log(self.name,
                                     f"futures OHLCV+OI archive ACTIVE for {sym} "
                                     f"— first bar {now_ist().strftime('%Y-%m-%d %H:%M')}. "
                                     f"Phase A could not run on the instrument; "
                                     f"this is what makes a re-test possible.")
                except Exception as e:
                    if should_log_throttled(self, "_fut_arch_fail", sym,
                                            f"{type(e).__name__}: {e}"):
                        self.bus.log(self.name,
                                     f"⚠ {sym}: futures candle archive FAILED "
                                     f"({type(e).__name__}: {e})")
            st[sym] = {"bucket": bucket, "o": ltp, "h": ltp, "l": ltp,
                       "c": ltp, "oi": oi,
                       "start_cum": cum_volume, "last_cum": cum_volume}
            return
        cur["h"] = max(cur["h"], ltp)
        cur["l"] = min(cur["l"], ltp)
        cur["c"] = ltp
        if oi is not None:
            cur["oi"] = oi
        if cum_volume is not None:
            cur["last_cum"] = cum_volume

    def _record_basis_residual(self, sym, fut_ltp):
        """One basis-residual observation per futures tick cycle.

        v59.0 Phase B §5. Deliberately hangs off the EXISTING futures
        tick path rather than adding a poll loop of its own — the Dhan
        pacing that path already respects is the whole reason it is
        safe to call this often.

        Failures are reported, not swallowed: this series is the evidence
        base for the residual signal exactly as the shadow journal is for
        the strategies, and Phase 0 spent a day proving that a silently
        missing table looks identical to a feature that was never wired.
        """
        try:
            import basis_residual as br
            import history as _h
            spot = (self.bus.get(f"analysis:{sym}") or {}).get("spot")
            if not spot or not fut_ltp:
                return
            exp = self.bus.get(f"future_expiry:{sym}")
            if not exp:
                return
            if hasattr(exp, "date"):
                dte = (exp.date() - now_ist().date()).days
            else:
                import datetime as _dt
                dte = (_dt.date.fromisoformat(str(exp)[:10]) - now_ist().date()).days
            hist = [r["residual"] for r in _h.basis_residual_series(sym, 1000)]
            obs = br.compute(sym, spot, fut_ltp, max(0, dte), hist)
            self.bus.set(f"basis_residual:{sym}", obs)
            _h.log_basis_residual(sym, time.time(), obs["spot"], obs["future"],
                                  obs["actual_basis"], obs["fair_basis"],
                                  obs["residual"], obs["residual_z"],
                                  obs["days_to_expiry"], obs["r_pct"],
                                  obs["q_pct"], obs["approx"])
        except Exception as e:
            if should_log_throttled(self, "_basis_fail", sym,
                                    f"{type(e).__name__}: {e}"):
                self.bus.log(self.name, f"⚠ {sym}: basis residual FAILED "
                                        f"({type(e).__name__}: {e})")

    def _build_volume_candle(self, sym, cum_volume, now=None):
        """Builds a real per-minute traded-volume series from the
        futures quote's cumulative-session-volume field — the delta
        between consecutive minute buckets, not the running total
        itself (a chart bar showing "total volume since market open"
        at every single bar would be meaningless — needs the amount
        traded WITHIN that specific minute). Mirrors _build_candle's
        own minute-bucketing technique exactly. Persists via
        history.upsert_volume_history(), keyed the same way as price
        candles ("{symbol}_SPOT_1m") so the two series line up
        one-to-one by timestamp for the chart."""
        now = now or time.time()
        bucket = int(now // 60) * 60
        state = getattr(self, "_volume_candle_state", None)
        if state is None:
            state = {}
            self._volume_candle_state = state
        prev = state.get(sym)
        if prev is None:
            state[sym] = {"bucket": bucket, "last_cum": cum_volume,
                         "bucket_start_cum": cum_volume}
            return
        if bucket != prev["bucket"]:
            # Minute rolled over — the just-completed bucket's traded
            # volume is however much the cumulative total grew during
            # it. A negative delta (cumulative counter reset, e.g. a
            # new session) is clamped to 0 rather than persisted as a
            # nonsensical negative volume.
            delta = max(0, prev["last_cum"] - prev["bucket_start_cum"])
            try:
                import history
                # Distinct "_FUT_1m" key, deliberately NOT the same
                # "_SPOT_1m" security_id price candles use — this is
                # FUTURES volume (the only real volume source; the
                # index itself has none), stored separately so it's
                # never confused with or accidentally overwrites the
                # index's own price-candle row. The chart merges the
                # two series by matching timestamp when serving history
                # (spot price + futures volume, a well-established
                # convention on this exact honest tradeoff — labeled
                # "Futures Volume" in the UI, not bare "Volume", so
                # it's never mistaken for the index's own volume).
                history.upsert_volume_history(f"{sym}_FUT_1m", prev["bucket"], delta)
            except Exception as e:
                key = f"_volume_persist_failed_{sym}"
                if not getattr(self, key, False):
                    setattr(self, key, True)
                    self.bus.log(self.name, f"⚠ failed to persist volume "
                                            f"for {sym}: {e}")
            state[sym] = {"bucket": bucket, "last_cum": cum_volume,
                         "bucket_start_cum": prev["last_cum"]}
        else:
            prev["last_cum"] = cum_volume

    def _ensure_ws_client(self, cfg):
        """Lazy singleton — created once, reused across cycles/symbols.
        Cooldown on repeated failure so a bad token doesn't retry every
        3s forever."""
        client = getattr(self, "_ws_client", None)
        if client is not None:
            return client
        fail_until = getattr(self, "_ws_fail_until", 0)
        if time.time() < fail_until:
            return None
        client_id = cfg.get("dhan_client_id")
        access_token = cfg.get("dhan_access_token")
        if not client_id or not access_token:
            self._ws_fail_until = time.time() + 60
            return None
        try:
            client = dhan_ws.DhanWebsocketClient(
                client_id, access_token,
                on_tick=self._on_ws_tick,
                on_status=lambda m: self.bus.log(self.name, f"ws: {m}"))
            for sym in self.bus.get("symbols", []):
                if sym.upper() in dhan_ws.INDEX_SECURITY_ID:
                    client.add_index_instrument(sym)
            client.start()
            self._ws_client = client
            self._ws_subscribed_legs = set()
            self.bus.log(self.name, "websocket market-data feed started "
                         "(hybrid mode: REST for chain shape/greeks, "
                         "websocket overlay for live LTP/OI/spot)")
            return client
        except Exception as e:
            self.bus.log(self.name, f"⚠ websocket feed failed to start: {e} "
                         f"— falling back to REST-only for 5 min")
            self._ws_fail_until = time.time() + 300
            return None

    def _on_ws_tick(self, sym, sec_id, tick):
        """Callback from the websocket client — runs on ITS thread, not
        this agent's cycle thread, so this must be safe to call anytime.
        Bus.set/get are already lock-protected (see Bus class)."""
        try:
            index_ids = {int(v) for v in dhan_ws.INDEX_SECURITY_ID.values()}
            if sec_id in index_ids:
                # index tick: spot-only update, no OI/depth to merge
                chain = self.bus.get(f"chain:{sym}")
                if not chain:
                    return   # REST hasn't fetched this symbol yet — wait for it
                chain["spot"] = tick["ltp"]
                self.bus.set(f"chain:{sym}", chain)
                tk = self.bus.get("ticker", {})
                if sym in tk:
                    tk[sym]["spot"] = tick["ltp"]
                    self.bus.set("ticker", tk)
                self._build_candle(sym, tick["ltp"])
                return
            future_map = getattr(self, "_future_sec_ids", {})
            if sec_id in future_map:
                role = getattr(self, "_future_roles", {}).get(sec_id, "front")
                if role == "front":
                    self._classify_future_tick(sym, tick)
                else:
                    self._future_month_tick(sym, sec_id, tick)
                return
            chain = self.bus.get(f"chain:{sym}")
            if not chain:
                return   # REST hasn't fetched this symbol's chain yet
            updated = dhan_ws.merge_tick_into_chain(chain, sec_id, tick)
            if updated:
                self.bus.set(f"chain:{sym}", chain)
        except Exception as e:
            self.bus.log(self.name, f"⚠ ws tick merge error: {e}")

    def _build_candle(self, sym, ltp):
        """Candle Builder Service (per the requested architecture:
        DhanHQ WS -> Market Data Service -> Candle Builder -> FastAPI
        WebSocket -> Lightweight Charts). Aggregates live spot ticks
        (already flowing through the existing websocket hybrid feed —
        no new subscription) into 1-minute candles.

        Publishes the CURRENTLY-FORMING candle to live_candle:{symbol}
        on every tick (what the WebSocket server pushes for a live-
        updating chart), and persists each COMPLETED minute to
        history.py's existing candles table (security_id convention
        "{symbol}_SPOT_1m", reusing the schema built for option-leg
        candles rather than adding a parallel table) the moment a new
        minute begins.
        """
        import history
        # 2026-07-26 — hard gate on market hours. Confirmed from live
        # screenshots: outside trading hours the websocket feed keeps
        # re-broadcasting the last known LTP as keepalive/reconnect
        # frames (a failure mode this codebase already documents in the
        # chart's _is_degenerate note). Each such tick landed here, was
        # built into a "new" flat 1m candle at a Saturday/Sunday
        # timestamp, PERSISTED to the DB, and streamed to the chart —
        # producing a flat multi-day tail after Friday's close that (a)
        # dragged the chart's visible window onto the tail so the view
        # opened effectively blank, (b) fed the indicator engines fake
        # flat bars (ATR visibly decaying toward zero across the
        # weekend), and (c) permanently contaminated the candles table.
        # A tick outside market hours is by definition not a trade, so
        # nothing here should run.
        if not fno_session_open():
            return
        now = time.time()
        minute = int(now // 60) * 60
        tracker = getattr(self, "_candle_1m", None)
        if tracker is None:
            tracker = {}
            self._candle_1m = tracker
        cur = tracker.get(sym)
        if not cur or cur["minute"] != minute:
            if cur:
                try:
                    history.upsert_candles(f"{sym}_SPOT_1m", [{
                        "ts": cur["minute"], "o": cur["open"], "h": cur["high"],
                        "l": cur["low"], "c": cur["close"], "v": None, "oi": None}])
                except Exception as e:
                    self.bus.log(self.name, f"⚠ candle persist failed for {sym}: {e}")
            cur = {"minute": minute, "open": ltp, "high": ltp, "low": ltp, "close": ltp}
            tracker[sym] = cur
        else:
            cur["high"] = max(cur["high"], ltp)
            cur["low"] = min(cur["low"], ltp)
            cur["close"] = ltp
        self.bus.set(f"live_candle:{sym}",
                     {"time": cur["minute"], "open": cur["open"],
                      "high": cur["high"], "low": cur["low"],
                      "close": cur["close"]})


class RegimeAgent(Agent):
    """Classifies today's market regime (trending / rangebound / choppy /
    gap-and-fade) and checks multi-timeframe alignment.

    Purpose: block bad trades before they happen. Last Monday's four
    losing BUY_CE trades all happened in a choppy/mean-reverting regime
    where buying calls is a slow bleed. This agent flags that.

    Publishes bus state 'regime:<sym>' consumed by the risk agent."""
    name = "regime"
    interval = 90     # candles don't move that fast; every 90s is plenty

    def cycle(self):
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None and fno_session_open():
            self.summary = "no broker client — set Dhan token"
            return
        # d may legitimately be None here when the market is closed —
        # _fetch_candles() falls back to this system's own persisted
        # candles in that case, so the panels still show the last
        # session rather than nothing at all.
        if not fno_session_open():
            # 2026-07-26 — was a hard early return ("market closed —
            # regime idle"), which left regime/bias/levels blank all
            # evening and weekend even though a full history of candles
            # is sitting in the DB. Per explicit request: "it is linked
            # with market, so market is closed — it should still show
            # the data based on available older dataset."
            #
            # Now it computes normally, but off the most recent AVAILABLE
            # session instead of today (see _session_only), and every
            # published result is tagged stale=True + session_date so no
            # consumer can mistake a Friday read for a live one. The
            # trade-gating consequences of that are handled explicitly
            # in RiskAgent.evaluate() and by withholding pa_candles
            # below — a stale regime must never gate a live trade.
            closed_note = "market closed — showing last session"
        else:
            closed_note = None
        syms = self.bus.get("symbols", ["NIFTY"])
        # compute regime for ALL symbols so switching tickers / trading any
        # index has fresh regime data (candle API is separate from the
        # option-chain rate limit; 12 light calls per 90s is fine)
        targets = syms
        done = []
        for sym in targets:
            if time.time() < self.bus.get(f"regime_fail_until:{sym}", 0):
                done.append(f"{sym[:4]}:skipped(see log)")
                continue
            try:
                r = self._classify(sym, d)
            except Exception as e:
                fails = self.bus.get(f"regime_fails:{sym}", 0) + 1
                self.bus.set(f"regime_fails:{sym}", fails)
                if fails <= 1 or fails % 10 == 0:
                    self.bus.log(self.name, f"{sym}: skipped ({e})")
                # 10 min backoff after repeated identical failures — this
                # is almost always a broker capability gap (e.g. Kotak has
                # no candle endpoint), not a transient blip worth retrying
                # every 90 seconds forever
                backoff = 600 if fails >= 3 else 90
                self.bus.set(f"regime_fail_until:{sym}", time.time() + backoff)
                done.append(f"{sym[:4]}:error")
                continue
            self.bus.set(f"regime_fails:{sym}", 0)
            if r:
                # 2026-07-26 — a last-session read is published to a
                # SEPARATE key, deliberately. `regime:{sym}` has 14
                # readers across this codebase, several of which feed
                # trade decisions (spread auto-deploy eligibility, ATR
                # stop/trail sizing, the risk gate's regime+confluence
                # checks). Requiring every one of them to remember a
                # `stale` check would be fragile and one missed call
                # site is a real-money bug. Keeping stale data off that
                # key entirely means all of them see "no regime data
                # yet" — a path every consumer already degrades
                # gracefully on — while the dashboard/API opt in
                # explicitly by reading regime_last_session:{sym}.
                if r.get("stale"):
                    self.bus.set(f"regime_last_session:{sym}", r)
                    done.append(f"{sym[:4]}:{r['regime'][:6]}@"
                                f"{r.get('session_date') or '?'}")
                else:
                    self.bus.set(f"regime:{sym}", r)
                    self.bus.set(f"regime_last_session:{sym}", r)
                    done.append(f"{sym[:4]}:{r['regime'][:6]}")
                self._compute_bias(sym, r)
            else:
                done.append(f"{sym[:4]}:warmup")
                # Log the reason once per change, not every 90s — this is
                # the diagnostic that was missing when levels appeared
                # for one symbol only and there was no way to tell why
                # the other three were in warm-up.
                reason = self.bus.get(f"regime_warmup_reason:{sym}")
                if reason and getattr(self, f"_warm_note_{sym}", None) != reason:
                    setattr(self, f"_warm_note_{sym}", reason)
                    self.bus.log(self.name, f"{sym}: regime in warm-up — {reason}")
            # 2026-07-26 — levels are now computed whether or not the
            # regime CLASSIFIED. Reported live: "Risk level are missing
            # for indexes" — the LTP Monitor showed R1-R3/S1-S3 for
            # NIFTY only, while the activity log confirmed all four
            # symbols reached _persist_candles inside _classify (so none
            # of them errored) and none logged a skip. The cause is
            # structural: _compute_levels() sat inside `if r:`, so any
            # symbol whose _classify() returned None — the warm-up
            # return, needing >=20 5m / >=8 15m / >=15 1m bars and >=3
            # bars of the session — silently got no levels either.
            # Levels don't actually depend on the regime at all: they
            # need analysis:{sym}'s OI walls, chain spot, and the
            # persisted daily OHLC. Nothing about a warming-up regime
            # makes them uncomputable, so they no longer wait for it.
            self._compute_levels(sym)
        self.summary = " · ".join(done) or "waiting for candles (needs ~15m after open)"
        if closed_note:
            self.summary = f"{closed_note} · {self.summary}"

    def _compute_bias(self, sym, regime):
        """Feature #2 (AI Market Bias) — extends this agent rather than
        adding a new one, since it already runs every 90s with fresh
        regime/candle data. Every input is read from bus keys other
        parts of this system already populate; only Supertrend/
        Ichimoku (inside market_bias.py) are newly computed here."""
        import market_bias as mb
        future_ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
        future_chg_pct = None
        if future_ohlc.get("close") and future_ohlc.get("open"):
            future_chg_pct = round(
                (future_ohlc["close"] - future_ohlc["open"])
                / future_ohlc["open"] * 100, 2)
        analysis = self.bus.get(f"analysis:{sym}") or {}
        result = mb.compute_bias(
            spot_chg_pct=regime.get("session_change_pct"),
            future_chg_pct=future_chg_pct,
            daily_candles=self.bus.get(f"regime_candles:{sym}"),
            regime=regime,
            oi_bias_pcr=analysis.get("pcr_oi"),
            future_trend=self.bus.get(f"future_oi_trend:{sym}"),
            vix=self.bus.get("india_vix"),
            global_sentiment=self.bus.get("global_risk_sentiment"),
        )
        self.bus.set(f"bias:{sym}", result)

    def _compute_levels(self, sym):
        """Feature #3 (Support/Resistance) — extends this agent the
        same way _compute_bias does: reuses regime_candles (no new API
        call) for previous-day levels, and analyzer.py's existing
        signal_lines (OI walls, retained as the primary S/R source,
        not recomputed) for the options-chain side."""
        import support_resistance as sr
        analysis = self.bus.get(f"analysis:{sym}") or {}
        chain = self.bus.get(f"chain:{sym}") or {}
        spot = chain.get("spot")
        if spot is None:
            return
        candles = self.bus.get(f"regime_candles:{sym}")
        self._persist_daily_ohlc(sym, spot)
        spot_ohlc_vwap = None  # spot VWAP is derived at the API layer
                               # from spot_hist, not tracked per-agent —
                               # merge_levels() below uses futures VWAP
                               # only, since that's what this agent has
                               # direct access to; the API layer's own
                               # response already carries spot VWAP
                               # separately for display.
        future_ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
        # 2026-07-26 — routed through sr.build_levels() (which wraps the
        # previous_day_levels + merge_levels pair this method used to
        # call inline). The chart websocket needs the identical merge as
        # an on-demand fallback outside market hours, when this agent is
        # idle and `levels:{sym}` is therefore never published; sharing
        # one function is what keeps the two from drifting apart.
        # build_levels() calls previous_day_levels() internally, which
        # this method used to call inline before _persist_daily_ohlc().
        # Order-independent: get_previous_day_ohlc() filters `date <
        # today`, so today's upsert can never become its own "previous
        # day" regardless of which runs first.
        levels = sr.build_levels(analysis, spot, candles, symbol=sym,
                                 future_vwap=future_ohlc.get("vwap"))
        self.bus.set(f"levels:{sym}", levels)

    def _persist_daily_ohlc(self, sym, spot):
        """Tracks today's running O/H/L/C in memory (same per-day-reset
        pattern already used for futures OHLC and OI baselines) and
        upserts to history.py's daily_ohlc table on every call —
        idempotent (REPLACE on the symbol+date key), so this just
        keeps today's row current as the session progresses. This is
        what makes today automatically become tomorrow's persisted
        "previous day" — no separate end-of-day job needed."""
        if spot is None:
            return
        import history
        today = now_ist().strftime("%Y-%m-%d")
        tracker = getattr(self, "_daily_ohlc_tracker", None)
        if tracker is None:
            tracker = {}
            self._daily_ohlc_tracker = tracker
        t = tracker.get(sym)
        if not t or t.get("date") != today:
            tracker[sym] = {"date": today, "open": spot, "high": spot,
                           "low": spot, "close": spot}
        else:
            t["high"] = max(t["high"], spot)
            t["low"] = min(t["low"], spot)
            t["close"] = spot
        t = tracker[sym]
        try:
            history.upsert_daily_ohlc(sym, today, t["open"], t["high"],
                                      t["low"], t["close"])
        except Exception as e:
            # DB write failing shouldn't take down regime/bias/levels
            # computation for this cycle — but it must not report only
            # ONCE EVER either. 2026-08-03: this latch never reset, so the
            # same failure on Monday and on Friday produced ONE line, and
            # a recovery followed by a relapse produced none. The inverse
            # of the success-latch problem two functions up, and the same
            # root cause: a per-process boolean standing in for state that
            # changes over time.
            #
            # The shared throttle re-reports when the ERROR CHANGES and
            # otherwise at most every 10 minutes, which keeps the 90s-cycle
            # spam away without turning a recurring fault into a one-off.
            if should_log_throttled(self, "_daily_ohlc_fail", sym,
                                    f"{type(e).__name__}: {e}"):
                self.bus.log(self.name, f"⚠ failed to persist daily OHLC "
                                        f"for {sym}: {e}")

    def _fetch_candles(self, d, sym, tf):
        """Dhan's intraday endpoint is rate-limited — pace every call.

        2026-07-26 — added a persisted-DB fallback for the market-closed
        path. The REST endpoint does normally return the last ~3 trading
        days even after hours, so this is a second line of defence
        rather than the main route: it's what makes the "show the last
        session from the available older dataset" behaviour hold up when
        no broker client is configured at all, or the broker is
        unreachable in the evening. Reads the same candles table
        RegimeAgent itself populates every cycle via _persist_candles,
        so it's this system's own recorded history, not a new source.

        During market hours nothing changes: any failure still raises,
        so cycle()'s existing per-symbol logging and 10-minute backoff
        keep working exactly as before rather than being masked by a
        silently-stale DB read.
        """
        time.sleep(1.2)
        try:
            candles = d.intraday(sym, tf)["candles"]
        except Exception:
            if fno_session_open():
                raise
            candles = []
        if not candles and not fno_session_open():
            import history
            candles = history.candles_before(f"{sym}_SPOT_{tf}m",
                                            time.time() + 1, 400)
            if candles:
                self.bus.log(self.name, f"{sym} {tf}m: broker returned no "
                             f"candles (market closed) — using "
                             f"{len(candles)} persisted bars from the "
                             f"local DB instead")
        return candles

    def _persist_candles(self, sym, c1, c5, c15):
        """Writes 1m/5m/15m candles to history.py's SQLite store for
        this symbol. Fail loud, not silent (project convention) —
        logged once per symbol rather than crashing the regime cycle
        or being swallowed, since a DB hiccup here shouldn't take down
        regime/bias/levels computation for the other symbols."""
        import history
        try:
            history.upsert_index_candles(sym, c1, 1)
            history.upsert_index_candles(sym, c5, 5)
            history.upsert_index_candles(sym, c15, 15)
        except Exception as e:
            key = f"_candle_persist_failed_{sym}"
            if not getattr(self, key, False):
                setattr(self, key, True)
                self.bus.log(self.name, f"⚠ failed to persist candles "
                                        f"for {sym}: {e}")

    def _today_only(self, candles):
        """Filter to candles whose timestamp falls on today's IST calendar
        date. Dhan's intraday fetch deliberately spans ~3 prior trading
        days (needed so ADX/ATR have enough bars to warm up early in the
        session) — but that means the FRONT of the array is NOT today's
        open. Anything computed as "today's opening range" or "today's
        session move" off the raw array is silently reading a blend of
        today and 1-2 prior days.

        Kept as a thin wrapper over _session_only() for any caller that
        genuinely wants strict today-or-nothing semantics."""
        return self._session_only(candles)[0]

    def _session_only(self, candles):
        """Isolate exactly ONE trading session's candles, and say which.

        Added 2026-07-26. Today's session when it exists; otherwise —
        and ONLY when the market is closed — the most recent session
        present in the data, so the regime/bias/levels panels show a
        real read of the last session instead of sitting blank all
        evening and weekend.

        The fno_session_open() condition is the important part and is a
        deliberate regression guard. The bug fixed earlier (candles
        from ~3 prior days being read as "today", producing a false
        "no-alignment" confluence for the first ~90 minutes of every
        session) is exactly what silently substituting an older session
        would reintroduce. So while the market IS trading and today's
        candles simply haven't accumulated yet, this returns EMPTY —
        a genuine warmup — rather than quietly handing back yesterday.

        Returns (candles_for_that_session, session_date_str_or_None).
        """
        today = now_ist().strftime("%Y-%m-%d")
        by_day = {}
        for c in candles or []:
            t = c.get("time")
            if t is None:
                continue
            d_str = datetime.fromtimestamp(t, IST).strftime("%Y-%m-%d")
            by_day.setdefault(d_str, []).append(c)
        if not by_day:
            return [], None
        if today in by_day:
            return by_day[today], today
        if fno_session_open():
            # Trading right now but no candles for today yet — real
            # warmup. Do NOT substitute an older session (see above).
            return [], None
        most_recent = max(by_day.keys())
        return by_day[most_recent], most_recent

    def _classify(self, sym, d):
        """Compute the regime label + multi-timeframe alignment.
        All from OHLC — no LLM, no extra API calls beyond candles."""
        # Fetch candles at three timeframes. Dhan intraday deliberately
        # returns ~3 prior trading days too (needed so ADX/ATR have
        # enough bars to warm up early in the session) — kept as-is for
        # those indicators. But "today's opening range" and "today's
        # session move" must NOT be computed off that raw multi-day
        # array, or the front of it (still-warming candles from
        # yesterday or before) gets silently read as today's open.
        c5 = self._fetch_candles(d, sym, "5")
        # Separate bus key (not bloating regime:{sym}, which many
        # consumers/API responses read and shouldn't have to carry a
        # large candle array) — Feature #2 (AI Market Bias) reuses
        # this same fetch for MACD/RSI/Supertrend/Ichimoku, no new
        # API call needed.
        self.bus.set(f"regime_candles:{sym}", c5)
        self.bus.set(f"pa_candles:{sym}", None)  # cleared; set correctly below
        c15 = self._fetch_candles(d, sym, "15")
        c1 = self._fetch_candles(d, sym, "1")
        # Persist all three timeframes for ALL symbols on every cycle
        # ("store the candles in local db for further use and
        # analysis, now onwards" — 2026-07-25). Reuses this exact
        # fetch, no new API calls. Deliberately persisted before the
        # warmup-length early-return below, so even a symbol still
        # warming up gets its candles captured rather than discarded.
        self._persist_candles(sym, c1, c5, c15)
        if len(c5) < 20 or len(c15) < 8 or len(c1) < 15:
            # Fail loud, not silent: "warmup" alone gave no way to tell
            # a genuine early-session warm-up from a symbol whose candle
            # feed is returning less than the others (which is what a
            # live report of levels missing for 3 of 4 symbols looked
            # like). Names the actual shortfall.
            self.bus.set(f"regime_warmup_reason:{sym}",
                         f"insufficient candles: 5m={len(c5)}/20 "
                         f"15m={len(c15)}/8 1m={len(c1)}/15")
            return None


        # 2026-08-04 — CAS filter. These three arrays are the ONLY source
        # of indicator input for the whole downstream chain: RegimeAgent's
        # own ATR/ADX, and `pa_candles:{sym}`, which PriceAction, S7, S8,
        # S9 and MTF all read. Filtering here rather than in each
        # indicator is the same reason the market-session check lives in
        # one function — a per-consumer copy is what drifts.
        #
        # The bars are NOT deleted; they stay in the archive and on the
        # chart. See strip_cas_frozen().
        c5_today, session_date = self._session_only(c5)
        c1_today, _ = self._session_only(c1)
        c15_today, _ = self._session_only(c15)
        c5_today = strip_cas_frozen(c5_today)
        c1_today = strip_cas_frozen(c1_today)
        c15_today = strip_cas_frozen(c15_today)
        if len(c5_today) < 3:
            # not enough of TODAY's session yet for a meaningful opening
            # range/session read, even though multi-day history exists
            self.bus.set(f"regime_warmup_reason:{sym}",
                         f"only {len(c5_today)} 5m bars in the "
                         f"{session_date or 'current'} session (need 3) "
                         f"— {len(c5)} bars available across all days")
            return None
        stale = session_date != now_ist().strftime("%Y-%m-%d")
        self.bus.set(f"regime_warmup_reason:{sym}", None)

        # pa_strategies.evaluate() explicitly requires "today's session
        # candles, oldest first" for ORB's opening-range window, the
        # VWAP-proxy anchor's cumulative mean, and EMA-MTF's cross
        # timing — all three assume index 0 is today's 9:15 open. Feeding
        # it the raw multi-day array (2-3 prior trading days blended in)
        # silently broke every one of those assumptions: the "opening
        # range" was really some prior day's mid-session candles, and the
        # anchor was a multi-day cumulative average, not today's. This is
        # the likely reason ORB/vwap_pullback/ema_mtf — all clearly
        # profitable in backtest, which correctly replays session-only
        # candles — never fired live.
        # 2026-07-26 — gated on `stale`. pa_candles feeds PriceAction
        # Agent, whose ORB opening-range window, VWAP-proxy anchor and
        # EMA-MTF cross timing ALL assume index 0 is TODAY's 9:15 open.
        # Handing it the last session's bars would let it emit a
        # genuine-looking breakout signal derived from Friday during
        # Monday's open (RegimeAgent's pre-open cycle runs while the
        # market is still closed, so the key would already be populated
        # before PriceActionAgent's own market_open() gate lifts).
        # Regime/bias/levels are safe to show from an older session
        # because they're read by humans and clearly labelled; a
        # tradeable price-action signal is not. Left as None so the
        # strategies simply don't evaluate until today's data exists.
        if stale:
            self.bus.set(f"pa_candles:{sym}", None)
        else:
            self.bus.set(f"pa_candles:{sym}", {"c1": c1_today, "c5": c5_today,
                                               "c15": c15_today,
                                               "ts": time.time()})
        # ---- Regime classification (based on 5m candles for today) ----
        # ATR (14) on 5m: proxy for volatility per bar — uses the full
        # multi-day series on purpose, ADX/ATR genuinely need the history
        atr14 = self._atr(c5, 14)
        # ADX (14) on 5m: trend strength — same, multi-day warmup is correct here
        adx14 = self._adx(c5, 14)
        # Opening range (first 15 min = first 3 x 5m candles) — TODAY only
        or_hi = max(c["high"] for c in c5_today[:3])
        or_lo = min(c["low"] for c in c5_today[:3])
        or_range = or_hi - or_lo
        curr = c5_today[-1]["close"]

        session_hi = max(c["high"] for c in c5_today)
        session_lo = min(c["low"] for c in c5_today)
        session_range = session_hi - session_lo

        # Where is price relative to opening range?
        or_position = ("above" if curr > or_hi else
                       "below" if curr < or_lo else "inside")
        # How much has the market travelled beyond the OR?
        or_expansion = ((session_range / or_range) if or_range > 0 else 1.0)

        # Directional bias from close-to-close over the session
        first_close = c5_today[0]["close"]
        session_change_pct = (curr - first_close) / first_close * 100

        # Whipsaw: number of sign flips in 5m candle direction over last 20
        recent = c5_today[-20:]
        directions = [1 if c["close"] > c["open"] else -1 for c in recent]
        flips = sum(1 for i in range(1, len(directions))
                    if directions[i] != directions[i-1])

        # ---- Regime label ----
        # Strong ADX + directional move + expansion out of OR = trending
        # Weak ADX + many flips + tight range = choppy/rangebound
        # Fast reversal from an OR break = gap-and-fade
        atr_pct = (atr14 / curr) * 100 if curr else 0

        if adx14 >= 25 and or_position != "inside" and or_expansion >= 1.5:
            regime = "trending-up" if session_change_pct > 0 else "trending-down"
            confidence = min(95, 50 + adx14)
        elif adx14 < 18 and flips >= 10:
            regime = "choppy"
            confidence = 60 + min(30, flips * 2)
        elif or_expansion < 1.3:
            regime = "rangebound"
            confidence = 70
        elif or_position == "inside" and flips >= 7:
            # broke OR then came back — classic fade
            regime = "gap-and-fade"
            confidence = 65
        else:
            regime = "mixed"
            confidence = 40

        # ---- Multi-timeframe confluence ----
        # Simple: is the trend direction the same on 1m, 5m, 15m?
        # TODAY-ONLY slices — using the raw multi-day arrays here was the
        # actual bug: comparing today's early-session move against a
        # "first third" that was really yesterday's (or older) closing
        # levels guarantees a false "no-alignment" read no matter how
        # cleanly today itself is trending.
        #
        # Note found 2026-07-22: even with correct today-only data,
        # 1m/5m/15m are legitimately DIFFERENT time windows (last 15min /
        # 75min / 120min of momentum) — they can disagree often during
        # completely normal intraday chop (a bounce within a pullback
        # within a trend), which made "no-alignment" the dominant outcome
        # for hours at a stretch even on a clearly trending day. Adding
        # the regime's own session-level directional read (already
        # computed above from the whole day's ADX/OR-expansion, a much
        # more stable signal) as a 4th vote lets a clear session trend
        # break a short-term-noise tie instead of every signal getting
        # blocked by momentary disagreement between three short windows.
        tf_bias = {
            "1m": self._trend_bias(c1_today[-15:]),
            "5m": self._trend_bias(c5_today[-15:]),
            "15m": self._trend_bias(c15_today[-8:]),
        }
        regime_vote = ("bull" if regime == "trending-up" else
                       "bear" if regime == "trending-down" else None)
        votes = list(tf_bias.values()) + ([regime_vote] if regime_vote else [])
        bulls = sum(1 for v in votes if v == "bull")
        bears = sum(1 for v in votes if v == "bear")
        total_votes = len(votes)
        if bulls == total_votes and bulls >= 3:
            confluence = "strong-bull"
        elif bears == total_votes and bears >= 3:
            confluence = "strong-bear"
        elif bulls >= 2 and bulls > bears:
            confluence = "mixed-bull"
        elif bears >= 2 and bears > bulls:
            confluence = "mixed-bear"
        else:
            confluence = "no-alignment"

        # ---- Trade playbook based on regime ----
        # This is what the risk agent will use to gate signals.
        allowed = {
            "trending-up":    ["BUY_CE"],
            "trending-down":  ["BUY_PE"],
            "rangebound":     [],                 # avoid directional bets
            "choppy":         [],                 # premium bleeds in chop
            "gap-and-fade":   ["BUY_CE", "BUY_PE"],  # both possible; needs confluence
            "mixed":          ["BUY_CE", "BUY_PE"],
        }.get(regime, [])

        return {
            "regime": regime,
            "confidence": int(round(confidence)),
            "adx": round(adx14, 1),
            "atr_pct": round(atr_pct, 2),
            "or_high": or_hi,
            "or_low": or_lo,
            "or_position": or_position,
            "or_expansion": round(or_expansion, 2),
            "session_change_pct": round(session_change_pct, 2),
            "flips_20bar": flips,
            "tf_bias": tf_bias,
            "confluence": confluence,
            "allowed_signals": allowed,
            "computed_at": now_ist().strftime("%H:%M:%S"),
            # 2026-07-26 — which session this read actually describes,
            # and whether that's today. Consumers MUST check `stale`
            # before treating any of the above as live: RiskAgent
            # downgrades a stale regime to "pending" so it can never
            # gate a live trade, and the dashboard labels the panel with
            # session_date instead of implying it's current.
            "session_date": session_date,
            "stale": stale,
        }

    def _atr(self, candles, n=14):
        trs = []
        prev_close = candles[0]["close"]
        for c in candles[1:]:
            tr = max(c["high"] - c["low"],
                     abs(c["high"] - prev_close),
                     abs(c["low"] - prev_close))
            trs.append(tr)
            prev_close = c["close"]
        if len(trs) < n:
            return sum(trs) / max(len(trs), 1)
        # simple moving ATR (Wilder's smoothing not needed for this use)
        return sum(trs[-n:]) / n

    def _adx(self, candles, n=14):
        """Simplified ADX — trend strength 0-100. Good enough to
        distinguish trending vs rangebound without a full library.

        2026-07-25 — refactored to call mtf_confluence_strategy.
        adx_di() (extracted from this exact method's own math) rather
        than duplicating it, so Feature #7's Technical Analysis Engine
        can reuse the identical calculation for its own +DI/-DI needs.
        Confirmed byte-for-byte identical output for the same input
        before shipping — this method's return value and behavior are
        completely unchanged."""
        import mtf_confluence_strategy as mcs
        adx, _pdi, _mdi = mcs.adx_di(candles, n)
        return adx

    def _trend_bias(self, candles):
        """Quick trend bias for one timeframe: compare last close to
        the middle-third average — resistant to a single-bar spike."""
        # Needs at least 3 candles: below that, `closes[:0]`/`closes[-0:]`
        # is a Python slicing gotcha (closes[-0:] is the WHOLE list, not
        # zero elements), which can produce a spurious "bull" read with
        # too little data to mean anything.
        if len(candles) < 3:
            return "flat"
        closes = [c["close"] for c in candles]
        first_third = sum(closes[:len(closes)//3]) / max(len(closes)//3, 1)
        last_third = sum(closes[-len(closes)//3:]) / max(len(closes)//3, 1)
        if last_third > first_third * 1.001:
            return "bull"
        if last_third < first_third * 0.999:
            return "bear"
        return "flat"


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
            snapshot_ctx = self._build_snapshot_ctx(sym)
            analysis = analyze(chain, momentum=momentum,
                               indicators=indicators, snapshot_ctx=snapshot_ctx)
            if analysis.get("error"):
                continue
            self.bus.set(f"analysis:{sym}", analysis)
            self._check_iv_spike(sym, analysis)
            self._maybe_persist_snapshot(sym, analysis)
            self._compute_institutional(sym, analysis)
            self._compute_technical(sym, analysis)
            done.append(f"{sym[:4]}:{analysis['bias'].split()[0][:4]}"
                        + (f"({momentum['trend'][:2]})" if momentum else ""))
            self.bus.publish("analysis", {"symbol": sym})
        self.summary = " · ".join(done) if done else "waiting for market data"
        self._maybe_snapshot_watch_symbols()

    def _compute_technical(self, sym, analysis):
        """Feature #7 (Technical Analysis Engine) — COMPLETE. All 13
        indicator engines (VWAP/EMA/MACD/RSI/ADX/ATR/Supertrend/
        Ichimoku/Bollinger/StochRSI/Momentum/Volume/MTF) plus the
        final Technical Score/Confirmation/AI Interpretation
        aggregation layer, via technical_engine.technical_output().
        Reuses regime_candles:{sym} (already fetched every 90s by
        RegimeAgent) and regime:{sym} (for the MTF engine) — no new
        API calls, no new candle fetch. Cross-checks against Feature
        #5's institutional_bias for the "technical does/doesn't
        confirm option chain activity" commentary. Wrapped in try/
        except so a problem here can never take down the option-chain/
        institutional analysis this runs alongside."""
        try:
            import technical_engine as te
            candles = self.bus.get(f"regime_candles:{sym}")
            vwap = self._spot_vwap_proxy(sym)
            regime = self.bus.get(f"regime:{sym}")
            output = te.technical_output(candles, analysis.get("spot"), vwap, regime)
            institutional = self.bus.get(f"institutional:{sym}") or {}
            output["ai_commentary"] = te.generate_technical_commentary(
                output, institutional.get("institutional_bias"))
            self.bus.set(f"technical:{sym}", output)
        except Exception as e:
            key = f"_technical_failed_{sym}"
            if not getattr(self, key, False):
                setattr(self, key, True)
                self.bus.log(self.name, f"⚠ technical engine failed "
                                        f"for {sym}: {e}")

    def _compute_institutional(self, sym, analysis):
        """Feature #5 (Institutional Activity Engine) — combines
        Feature #2's bias:{sym} (already-computed technical/spot/
        futures synthesis) with this cycle's analysis (Feature #4's
        option-chain intelligence, just computed above) into one
        cross-domain read. Every input reused, nothing recomputed —
        see institutional_engine.py's own module docstring for the
        full mapping. Wrapped in try/except so a problem here can
        never take down the option-chain analysis this depends on."""
        try:
            import institutional_engine as ie
            bias = self.bus.get(f"bias:{sym}")
            vwap = self._spot_vwap_proxy(sym)
            regime = self.bus.get(f"regime:{sym}")
            future_oi_trend = self.bus.get(f"future_oi_trend:{sym}")
            result = ie.institutional_output(
                bias, analysis, spot=analysis.get("spot"), vwap=vwap,
                regime=regime, future_oi_trend=future_oi_trend)
            self.bus.set(f"institutional:{sym}", result)
        except Exception as e:
            key = f"_institutional_failed_{sym}"
            if not getattr(self, key, False):
                setattr(self, key, True)
                self.bus.log(self.name, f"⚠ institutional engine failed "
                                        f"for {sym}: {e}")

    def _spot_vwap_proxy(self, sym):
        """Same TWAP-proxy convention already established elsewhere in
        this codebase (AnchorPullback's "session anchor", Feature #1's
        spot VWAP) — mean of today's spot ticks, no new tracking added;
        reuses spot_hist:{sym}, which MarketDataAgent already
        accumulates every REST cycle."""
        hist = self.bus.get(f"spot_hist:{sym}", [])
        if not hist:
            return None
        today = now_ist().strftime("%Y-%m-%d")
        todays = [v for ts, v in hist
                 if datetime.fromtimestamp(ts, IST).strftime("%Y-%m-%d") == today]
        if not todays:
            return None
        return sum(todays) / len(todays)

    def _build_snapshot_ctx(self, sym):
        """Feature #4 (Option Chain Intelligence Engine) — fetches the
        two comparison snapshots analyze()'s change-vs-previous/
        change-vs-session-open calculations need, ONCE per cycle per
        symbol (not per-strike), per the spec's own Performance
        section ("cache calculations... avoid full recalculation on
        every tick"). Returns {} gracefully if history.py isn't
        importable or nothing's persisted yet — analyze() already
        treats an empty/missing snapshot_ctx as "no comparison
        available", not an error."""
        try:
            import history
            session_start = int(datetime.strptime(
                now_ist().strftime("%Y-%m-%d"), "%Y-%m-%d")
                .replace(tzinfo=IST).timestamp())
            now_ts = int(time.time())
            return {
                "prev": history.get_chain_snapshot_map(sym, now_ts),
                "session_open": history.get_chain_session_open_map(sym, session_start),
            }
        except Exception:
            return {}

    def _maybe_persist_snapshot(self, sym, analysis):
        """Persists a chain snapshot at most once every
        chain_snapshot_interval_sec (config, default 60s) per symbol —
        NOT on every TechnicalAgent cycle (which itself already runs
        every 60s, but this stays independently throttled in case that
        interval is ever tightened, per the spec's explicit ask for a
        configurable 30s/1m/5m/15m cadence)."""
        import config as _cfg
        interval = _cfg.load().get("chain_snapshot_interval_sec", 60)
        last = getattr(self, "_last_snapshot_ts", None)
        if last is None:
            last = {}
            self._last_snapshot_ts = last
        now_ts = time.time()
        if now_ts - last.get(sym, 0) < interval:
            return
        try:
            import history
            history.upsert_chain_snapshot(sym, now_ts, analysis.get("strikes", []))
            last[sym] = now_ts
        except Exception as e:
            key = f"_snapshot_persist_failed_{sym}"
            if not getattr(self, key, False):
                setattr(self, key, True)
                self.bus.log(self.name, f"⚠ failed to persist chain "
                                        f"snapshot for {sym}: {e}")

    def _maybe_snapshot_watch_symbols(self):
        """Per-strike OI/IV/greeks archive for watchlist names.
        ARCHIVE ONLY — these symbols are never traded.

        2026-08-06. The watch loop in BacktestAgent (see its comment at
        the `watch_symbols` block) archives option-leg and futures
        CANDLES once a day. That is price and nothing else. Its own
        stated purpose is that a candidate instrument accumulates
        history so "its liquidity can be measured" before anyone
        decides whether it is worth trading — and liquidity is OI,
        volume and bid/ask, every one of which lives in
        `chain_snapshots` and none of which lives in `candles`. As
        shipped the archive could not answer the question it exists to
        answer: on 2026-08-06 ADANIENSOL had 803 candle rows and ZERO
        snapshot rows, while each index had ~30k.

        DELIBERATELY SLOWER THAN THE INDEX CADENCE (300s vs 60s). The
        option-chain endpoint is shared with the four traded symbols
        and is rate-limited; it returned 429 at 09:31 on 2026-08-06.
        A name that is never traded must not spend budget the traded
        ones need, and if this path does cause contention the symptom
        lands on NIFTY/BANKNIFTY analysis rather than here.

        The symbol is NEVER added to the bus "symbols" list and no
        `analysis:{sym}` key is written. Those drive strategy, risk and
        execution; a name reaching either would be traded. This writes
        to the archive and nothing else.
        """
        import config as _cfg
        cfg = _cfg.load()
        watch = cfg.get("watch_symbols") or []
        if not watch or not fno_session_open():
            return
        interval = cfg.get("watch_snapshot_interval_sec", 300)
        last = getattr(self, "_last_watch_snapshot_ts", None)
        if last is None:
            last = {}
            self._last_watch_snapshot_ts = last
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            return
        import rate_limit
        for wsym in watch:
            now_ts = time.time()
            if now_ts - last.get(wsym, 0) < interval:
                continue
            # Own resource name: a 429 here backs THIS path off without
            # touching the traded symbols' chain fetches.
            if rate_limit.is_limited("watch_chain"):
                return
            try:
                chain = d.option_chain(wsym)
                analysis = analyze(chain)
                if analysis.get("error"):
                    continue
                import history
                n = history.upsert_chain_snapshot(
                    wsym, now_ts, analysis.get("strikes", []))
                last[wsym] = now_ts
                if not getattr(self, f"_watch_snap_ok_{wsym}", False):
                    setattr(self, f"_watch_snap_ok_{wsym}", True)
                    self.bus.log(self.name,
                                 f"watch {wsym}: chain snapshots ACTIVE "
                                 f"({n} rows/snapshot, every {interval}s) "
                                 f"— archive only, never traded")
            except Exception as e:
                rate_limit.note_failure(e, "watch_chain",
                                        on_429=300, otherwise=60)
                last[wsym] = now_ts     # do not retry instantly
                if not getattr(self, f"_watch_snap_failed_{wsym}", False):
                    setattr(self, f"_watch_snap_failed_{wsym}", True)
                    self.bus.log(self.name,
                                 f"⚠ watch {wsym}: chain snapshot failed "
                                 f"({type(e).__name__}: {str(e)[:120]})")

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

    def cycle(self):
        # 2026-07-24: was a single hardcoded Google-News RSS query,
        # scraped with a raw regex. Now pulls from every enabled feed
        # in news_engine's shared config (the user's confirmed Indian
        # sources plus global feeds), each headline categorized and
        # bias-scored, and logged into the SAME shared, deduplicated
        # tracker NewsMacroAgent writes into — this is the actual fix
        # for "picking similar information again and again": both
        # agents now draw from one shared, deduplicated pipeline
        # instead of two independent ones repeatedly processing the
        # same underlying stories.
        #
        # Everything below this point (the AI sentiment/risk_event
        # classification, the "news" bus key shape, the cooldown/
        # state-transition alerting) is UNCHANGED from before this
        # change — that logic is close to live risk-gating
        # (news_risk_opportunity() reads exactly this bus key) and was
        # deliberately left untouched beyond swapping the input source.
        import news_engine as ne
        events, errors = ne.fetch_all_enabled(max_items_per_feed=8)
        for err in errors:
            self.bus.log(self.name, f"⚠ feed fetch failed: {err['feed']} — {err['error']}")
        for evt in events:
            ne.log_tracked_event(evt)   # shared, deduped tracker for the dashboard
        # Bug found 2026-07-24: prune_tracker_file() existed but was
        # never actually called anywhere — retention was unbounded in
        # practice, confirmed live (1000+ accumulated entries). Runs at
        # most once/hour here, not every 15-min cycle, since it's a
        # full-file rewrite and doesn't need to run more often than that.
        if time.time() - getattr(self, "_last_prune", 0) > 3600:
            self._last_prune = time.time()
            ne.prune_tracker_file()
        heads = [e["description"] for e in events if e.get("valid")][:15]
        if not heads:
            self.summary = "no headlines fetched"
            return
        # Bug found 2026-07-22: the same handful of headlines kept
        # re-triggering "News risk event" alerts every cycle from ~3pm
        # onward. Root cause — the RSS feed wasn't actually returning
        # new content, but the LLM call was still re-run on the exact
        # same headlines every cycle, and an occasional parse/auth
        # failure would knock risk_event to False for one cycle, then
        # the next (identical-content) cycle flipped it back to True —
        # a false "not-active -> active" edge on stale data, firing a
        # duplicate alert with the same note. Fix: skip re-analysis
        # entirely when the headline set hasn't changed, and validate
        # that the feed is actually updating rather than silently
        # re-processing the same content.
        sig = hash(tuple(sorted(heads)))
        prev_sig = self.bus.get("news_headline_sig")
        if sig == prev_sig:
            stale_since = self.bus.get("news_stale_since") or time.time()
            self.bus.set("news_stale_since", stale_since)
            stale_minutes = (time.time() - stale_since) / 60
            prev = self.bus.get("news") or {}
            if stale_minutes >= 120 and not self.bus.get("news_stale_warned"):
                self.bus.log(self.name,
                             f"⚠ feed has returned identical headlines for "
                             f"{stale_minutes:.0f} min — not re-analyzing or "
                             f"re-alerting on unchanged content")
                self.bus.set("news_stale_warned", True)
            self.summary = (f"{prev.get('sentiment','neutral')} · "
                           f"risk_event={prev.get('risk_event', False)} "
                           f"(unchanged {stale_minutes:.0f}m)")
            return
        self.bus.set("news_headline_sig", sig)
        self.bus.set("news_stale_since", time.time())
        self.bus.set("news_stale_warned", False)
        if config.load().get("ai_engine", "local") != "off":
            try:
                # risk_event is DEFINED here (2026-08-08). It used to be
                # asked for with no definition at all, and the model
                # answered "true" for mixed conditions, for purely
                # positive news, and once for the placeholder itself —
                # 33 of 114 flagged events. `note` no longer uses angle
                # brackets, because "<one line>" came back verbatim 6
                # times and was flagged as a risk event.
                out = claude(
                    "Market news headlines for Indian indices below.\n"
                    "risk_event means a SPECIFIC, DATEABLE event likely to "
                    "move the index in the next hour — a rate decision, "
                    "geopolitical escalation, circuit breaker, large "
                    "default, or similar. General conditions, mixed "
                    "sentiment, routine moves and positive news are NOT "
                    "risk events; answer false for those.\n"
                    "Reply ONLY JSON: {\"sentiment\":\"bullish|bearish|"
                    "neutral\",\"risk_event\":true|false,\"note\":\"one "
                    "short sentence naming the event\"}\n\n"
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
        # The prompt above asks for a definition; this ENFORCES it.
        # Load-bearing rather than belt-and-braces: ai_engine may be
        # "off" or unreachable, prompts drift, and a model is free to
        # ignore an instruction. news_engine owns the wording rules —
        # extended there, not re-implemented here, because the news
        # sentiment logic has already been forked once in this codebase
        # and had to be collapsed back to one definition.
        if j.get("risk_event"):
            import news_engine as _ne
            _material, _why = _ne.is_material_risk_event(j.get("note"))
            if not _material:
                j["risk_event"] = False
                j["risk_event_downgraded"] = _why
                self.bus.log(self.name,
                             f"news risk_event downgraded — {_why}: "
                             f"{str(j.get('note'))[:90]!r}")
        j["headlines"] = heads[:8]
        # State-transition alerting: alert when a risk event first
        # appears, and only re-alert periodically (a cooldown) while it
        # remains ongoing — never on every cycle.
        #
        # Bug found 2026-07-22: comparing exact note TEXT (as a proxy for
        # "is this a new event") doesn't work — the LLM naturally rewords
        # a continuing, unchanged market condition slightly every single
        # cycle ("indices experienced significant declines across news
        # headlines" -> "have experienced significant declines in the
        # recent news headlines" -> "fell significantly due to
        # geopolitical tensions and oil prices" -- all the SAME ongoing
        # afternoon selloff, just reworded), so note_changed was true
        # almost every cycle and fired a fresh alert every 15-30 minutes
        # all afternoon. A cooldown is the only guard that doesn't
        # depend on the LLM's wording being stable.
        cfg = config.load()
        prev = self.bus.get("news") or {}
        was_active = bool(prev.get("risk_event"))
        now_active = bool(j.get("risk_event"))
        if now_active:
            # Bug found 2026-07-22: `(not was_active) or (cooldown expired)`
            # let risk_event's occasional flip to False — ordinary
            # classification noise across a headline change, not a real
            # resolution of the event — re-arm an IMMEDIATE alert next
            # cycle regardless of the cooldown. Observed cadence was
            # ~30min despite a 60min cooldown, exactly consistent with
            # every other 15min headline-change cycle tripping this.
            # The cooldown alone is the only guard that can't be
            # defeated by noise: `last_alert_ts` starts at 0, so the
            # very first alert still fires immediately.
            cooldown_sec = cfg.get("news_realert_cooldown_minutes", 60) * 60
            last_alert_ts = self.bus.get("news_last_alert_ts", 0)
            should_alert = (time.time() - last_alert_ts) >= cooldown_sec
            if should_alert:
                j["flagged_ts"] = time.time()
                self.bus.set("news_last_alert_ts", time.time())
                self.bus.alert("high", "news", "",
                               f"News risk event: {j.get('note','')}")
            else:
                # same ongoing event, still within the cooldown — keep the
                # original timestamp so risk agent's expiry window is
                # measured from when the event was first detected, not
                # re-armed every cycle just because the wording shifted
                j["flagged_ts"] = prev.get("flagged_ts", time.time())
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
                # 2026-07-30 -- the dashboard showed "retail mood: {".
                # The prompt says "One word only" and the model replied
                # with JSON, so split()[0] took the opening brace and
                # rendered it as the mood. Nothing validated the reply
                # against the prompt's own stated contract.
                #
                # THIRD instance of this class in one session: the
                # `signal` field check, then momentum_buy's RR/strike
                # invariants (v58.44), now this. A prompt is a request,
                # not a guarantee -- any LLM output used downstream needs
                # validating at the boundary.
                VALID = ("euphoric", "bullish", "neutral", "bearish", "fearful")
                raw = (out or "").strip().lower()
                mood = next((w for w in VALID if w in raw), "unknown")
                if mood == "unknown" and raw:
                    self.bus.log(self.name,
                                 f"social: model did not return one of "
                                 f"{VALID} -- got {raw[:60]!r}, using 'unknown'")
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
        cfg = config.load()
        max_pos = cfg.get("max_concurrent_positions", 1)
        positions = self.bus.get("positions", {}) or {}
        if len(positions) >= max_pos:
            self.summary = f"{len(positions)}/{max_pos} positions open — no new signals"
            return
        # never signal again on a symbol that already has an open position
        jobs = {s: j for s, j in jobs.items() if s not in positions}
        if not jobs:
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
            # 2026-08-03 — atr_pct lives in regime:{sym}, NOT in the
            # analysis/chain dict. analyzer.option_stop_geometry reads
            # analysis.get("atr_pct"), found nothing, and every option
            # stop silently took the flat 30% fallback — volatility had
            # no influence on stop placement at all. Passed through a
            # COPY so the shared bus object is not mutated.
            _reg = self.bus.get(f"regime:{sym}") or {}
            if _reg.get("atr_pct"):
                analysis = dict(analysis, atr_pct=_reg["atr_pct"])
            # 2026-08-03 — the repair layer's log had nowhere to go: this
            # call omitted `log`, so enforce_signal_invariants wrote to
            # its no-op default. Verified live: 34 journal trades carry
            # the ai_signal schema, so the layer HAS been running on real
            # trades since v58.44 and left no trace of a single repair.
            sig = ai_signal(analysis, context=context,
                            log=lambda m, _s=sym: self.bus.log(self.name,
                                                               f"{_s}: {m}"))
            self.bus.set(f"signal:{sym}", sig)
            if sig["signal"] != "WAIT" and \
               (best is None or sig["confidence"] > best[1]["confidence"]):
                best = (sym, sig, analysis)
        if best:
            sym, sig, analysis = best
            sig["symbol"] = sym
            self.bus.set("last_signal", sig)
            self._last_signal_ts = time.time()
            trade_params = (f"entry ₹{sig.get('entry')} SL ₹{sig.get('stoploss')} "
                           f"T1 ₹{sig.get('target1')} T2 ₹{sig.get('target2')}")
            self.summary = (f"{sym}: {sig['signal']} {sig.get('strike','')} "
                          f"conf {sig['confidence']}% — {trade_params}")
            self.bus.log(self.name, self.summary)
            self.bus.alert("medium", "strategy", sym,
                           f"{sig['signal'].replace('_',' ')} {sig.get('strike','')} "
                           f"signal generated (confidence {sig['confidence']}%) — {trade_params}")
            self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                        "analysis": analysis})
        else:
            self.summary = f"scanned {len(jobs)} indices — WAIT"


SHADOW_PATH = store.path("shadow_signals.jsonl")


# v59.0 — counts journal rows lost to OS errors. A single mutable cell so
# the count survives across calls without a module-level global rebind.
_shadow_write_failures = [0]


def log_futures_shadow(bus, sym, side, gates, taken, why=None, ltp=None,
                       lots=None, stop=None, target=None):
    """Shadow-journal every futures EVALUATION, taken or not.

    2026-07-29 — options and Strategy 7 have had a shadow journal since
    v51; futures never did. So the one instrument class that is
    demonstrably losing money (40 trades, 27.5% win, -₹23,863) was also
    the only one whose REJECTED signals left no record. There was no way
    to ask "was the gate right to block that?" for futures, and no
    volume for the ML probability model either.

    This matters more now than it did before v58.39: the futures
    per-trade rupee cap and the ADX gate will both refuse trades, and
    without a record of what they refused there is no way to tell a
    working filter from an over-tight one.

    Reuses the same JSONL the options journal writes to, tagged
    kind="futures", so one reader serves both.
    """
    entry = {
        "id": f"{sym}-FUT-{int(time.time()*1000)}",
        "ts": now_ist().isoformat(), "symbol": sym, "kind": "futures",
        "signal": f"FUT_{side}" if side else "FUT_NONE",
        "entry": ltp, "stoploss": stop, "target1": target,
        "lots": lots, "gates": gates,
        "failed_gates": [k for k, v in (gates or {}).items()
                         if isinstance(v, str) and
                         ("blocked" in v.lower() or "skipped" in v.lower())],
        "verdict": "APPROVED" if taken else "REJECTED",
        "why": why,
        "resolution": "taken" if taken else "pending",
    }
    # v59.0 Phase 0 §3.3 — this was `except Exception: pass`.
    #
    # The shadow journal is the evidence base for the entire futures
    # research project: it is how "was the gate right to block that?"
    # gets answered, and 142 of 183 evaluations recorded so far are
    # REJECTED signals that exist nowhere else. Silently dropping an
    # entry does not degrade the analysis, it biases it — the entries
    # most likely to fail a write (disk full, permissions, a serialisation
    # error on an unusual gate value) are not a random sample.
    #
    # A journal that loses rows without saying so is worse than no
    # journal, because it still looks complete. Transient OS errors are
    # surfaced on the bus and counted; a programming error (TypeError
    # from a non-serialisable gate value) propagates per the fail-loud
    # rule.
    # Serialisation is a PROGRAMMING error and must propagate. I/O is an
    # environment error and must be reported without killing the trading
    # loop — so os.makedirs belongs with the I/O, not with the encoding.
    # It was originally in the block below and a permissions failure
    # escaped uncaught, which the test caught: fail-loud must still mean
    # survivable for the caller that is holding a position.
    try:
        line = json.dumps(entry)
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"futures shadow entry is not serialisable ({type(e).__name__}: {e}) "
            f"— gates={gates!r}. Fix the caller; do not drop the record.") from e
    try:
        os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
        with open(SHADOW_PATH, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        _shadow_write_failures[0] += 1
        msg = (f"⚠ futures shadow journal WRITE FAILED "
               f"({type(e).__name__}: {e}) — {_shadow_write_failures[0]} lost "
               f"so far. The journal is the evidence base for futures "
               f"research; it is now incomplete.")
        try:
            bus.log("shadow", msg)
            bus.alert("high", "shadow", sym or "", "futures journal write failed")
        except Exception:
            print("  " + msg)
    return entry


def _log_shadow_signal(bus, job, verdict, checks):
    """Persist every signal decision — approved AND rejected — so we can
    later ask 'was the risk agent right to reject that?' not just
    review the trades that were actually taken. Rejected signals get a
    pending resolution tracked forward against real subsequent prices."""
    sig, sym = job["signal"], job["symbol"]
    entry = {
        "id": f"{sym}-{int(time.time()*1000)}",
        "ts": now_ist().isoformat(), "symbol": sym,
        "signal": sig["signal"], "strike": sig.get("strike"),
        "entry": sig.get("entry"), "stoploss": sig.get("stoploss"),
        "target1": sig.get("target1"), "target2": sig.get("target2"),
        "confidence": sig.get("confidence"), "verdict": verdict,
        "checks": checks, "failed_checks": [c for c in checks if c.startswith("✗")],
        # Strategy 7 (v51): per-gate pass/fail breakdown — the paper-
        # first validation path the spec requires, and the input the ML
        # probability scoring (roadmap #7/#12) is waiting on for volume.
        "source": sig.get("source"),
        "s7_gates": sig.get("s7_gates"),
        "resolution": "taken" if verdict == "APPROVED" else "pending",
    }
    # 2026-08-03 — this was `except Exception: pass`, twenty lines below
    # the comment explaining why that exact pattern was removed from the
    # FUTURES writer above (v59.0 Phase 0 §3.3). The reasoning transfers
    # unchanged and was simply never applied here: the entries most
    # likely to fail a write are not a random sample, so dropping them
    # silently does not degrade the record, it BIASES it — and a journal
    # that loses rows without saying so still looks complete.
    #
    # This half is the larger evidence base of the two: 389 attributed
    # option/price-action signals, against which "was the risk agent
    # right to reject that?" is asked. Same split as the futures writer —
    # serialisation is a programming error and propagates; I/O is an
    # environment error, reported without killing a loop that may be
    # holding a position.
    try:
        line = json.dumps(entry)
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"shadow entry is not serialisable ({type(e).__name__}: {e}) "
            f"— signal={sig!r}. Fix the caller; do not drop the record.") from e
    try:
        os.makedirs(os.path.dirname(SHADOW_PATH), exist_ok=True)
        with open(SHADOW_PATH, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        _shadow_write_failures[0] += 1
        msg = (f"⚠ shadow journal WRITE FAILED ({type(e).__name__}: {e}) — "
               f"{_shadow_write_failures[0]} lost so far. Rejected signals "
               f"exist nowhere else; the record is now incomplete.")
        try:
            bus.log("shadow", msg)
            bus.alert("high", "shadow", sym or "", "shadow journal write failed")
        except Exception:
            print("  " + msg)
    if verdict == "REJECTED":
        pending = bus.get("shadow_pending", [])
        pending.append(entry)
        bus.set("shadow_pending", pending[-100:])   # bounded, most-recent


def _resolve_shadow_signals(bus, get_chain):
    """Called periodically: check whether rejected signals, HAD they
    been taken, would have hit target or stoploss — using the same
    strike's real subsequent LTP. Times out after 90 minutes as
    'unresolved' (regime shifted enough that the hypothetical no
    longer means much)."""
    pending = bus.get("shadow_pending", [])
    if not pending:
        return
    still_pending = []
    resolved_updates = []
    for e in pending:
        age_min = (time.time() - datetime.fromisoformat(e["ts"]).timestamp()) / 60 \
            if isinstance(e["ts"], str) else 0
        try:
            chain = bus.get(f"chain:{e['symbol']}") or get_chain(e["symbol"])
            row = next((r for r in chain["rows"] if r["strike"] == e["strike"]), None)
            leg = "ce" if "CE" in e["signal"] else "pe"
            ltp = row[leg].get("ltp") if row else None
        except Exception:
            ltp = None
        outcome = None
        if ltp:
            if ltp >= (e.get("target1") or 1e18):
                outcome = "would_have_hit_target1"
            elif ltp <= (e.get("stoploss") or -1):
                outcome = "would_have_hit_stoploss"
        if not outcome and age_min > 90:
            outcome = "unresolved_timeout"
        if outcome:
            resolved_updates.append({**e, "resolution": outcome,
                                     "resolved_ltp": ltp,
                                     "resolved_at": now_ist().isoformat()})
        else:
            still_pending.append(e)
    bus.set("shadow_pending", still_pending)
    if resolved_updates:
        try:
            lines = open(SHADOW_PATH).readlines() if os.path.exists(SHADOW_PATH) else []
            by_id = {json.loads(l)["id"]: l for l in lines if l.strip()}
            for u in resolved_updates:
                by_id[u["id"]] = json.dumps(u) + "\n"
            with open(SHADOW_PATH, "w") as f:
                f.writelines(by_id.values())
        except Exception:
            pass


def news_risk_opportunity(news, signal_direction, cfg):
    """Roadmap item: replace the blanket news-risk block window with
    directional risk/opportunity scoring.

    The old gate blocked EVERY signal for `news_block_minutes` after any
    flagged news risk event, regardless of which way the news actually
    pointed — a bearish headline blocked BUY_PE (the trade it should have
    supported) just as hard as BUY_CE. This scores the news against the
    proposed trade's direction instead:

      - Returns (blocks, note, score). score is in [-1, 1]:
          score < 0  -> news conflicts with this trade direction (risk)
          score > 0  -> news supports this trade direction (opportunity,
                        never blocks — a bearish headline is exactly the
                        kind of signal a BUY_PE should be allowed to act on)
          score == 0 -> no directional read (neutral sentiment, or the
                        event has aged out)
      - The effect decays linearly from full strength at the moment the
        event was flagged to zero at `news_block_minutes` — a hard cliff
        at the deadline was replaced with a fading influence, so a trade
        proposed at 19 minutes isn't treated identically to one at 1
        minute.
      - Only a CONFLICTING direction can block; an aligned or neutral
        direction never does.
    """
    if not news.get("risk_event"):
        return False, "no active news risk", 0.0
    flagged_ts = news.get("flagged_ts", 0)
    window = max(1, cfg.get("news_block_minutes", 20))
    age_min = (time.time() - flagged_ts) / 60
    if age_min >= window:
        return False, f"news risk expired ({age_min:.0f}m > {window}m window)", 0.0
    decay = max(0.0, 1 - age_min / window)
    sentiment = news.get("sentiment", "neutral")
    wants_bull = signal_direction == "BUY_CE"
    wants_bear = signal_direction == "BUY_PE"
    if sentiment == "bearish" and wants_bull:
        return True, (f"bearish news conflicts with CE buy "
                      f"({age_min:.0f}m old, strength {decay:.2f})"), -decay
    if sentiment == "bullish" and wants_bear:
        return True, (f"bullish news conflicts with PE buy "
                      f"({age_min:.0f}m old, strength {decay:.2f})"), -decay
    if sentiment == "bearish" and wants_bear:
        return False, (f"bearish news aligns with PE buy — opportunity, "
                       f"not blocked (strength {decay:.2f})"), decay
    if sentiment == "bullish" and wants_bull:
        return False, (f"bullish news aligns with CE buy — opportunity, "
                       f"not blocked (strength {decay:.2f})"), decay
    return False, f"neutral-direction news risk ({age_min:.0f}m old) — no directional block", 0.0


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
        # v59.69 — daily_pnl is no longer accumulated here: it is derived
        # from closed_trades via realized_pnl_today() at every use, which
        # survives restarts and rolls over at midnight without a reset
        # hook. This handler now only tracks the consecutive-loss halt.
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
        # v59.69 — refresh from the one shared definition before any
        # check reads it (see realized_pnl_today's docstring).
        self.daily_pnl = realized_pnl_today(self.bus)
        sig, cfg = job["signal"], config.load()
        checks = []
        ok = True

        def check(cond, label):
            nonlocal ok
            checks.append(("✓" if cond else "✗") + " " + label)
            ok = ok and cond

        trades = self.bus.get("trades_today", 0)
        check(not self.halted, "autopilot not halted (consecutive losses)")
        portfolio_halted_until = self.bus.get("portfolio_halt_until", 0)
        if portfolio_halted_until:
            remaining = (portfolio_halted_until - time.time()) / 60
            check(remaining <= 0,
                 f"portfolio kill-switch cooldown ({remaining:.0f}m remaining)")
        cooldown_min = cfg.get("cooldown_after_loss_min", 15)
        if cooldown_min > 0 and self.last_loss_ts:
            since_loss = (time.time() - self.last_loss_ts) / 60
            check(since_loss >= cooldown_min,
                  f"cooldown ({since_loss:.0f}m/{cooldown_min}m since last loss)")
        check(market_open(), "market open")
        positions = self.bus.get("positions", {}) or {}
        max_pos = cfg.get("max_concurrent_positions", 1)
        check(job["symbol"] not in positions,
              f"no open position on {job['symbol']}")
        check(len(positions) < max_pos,
              f"concurrent positions {len(positions)}/{max_pos}")
        check(trades < cfg["max_trades_per_day"],
              f"trades {trades}/{cfg['max_trades_per_day']}")
        # The label must describe the PASSING state, like every other
        # gate on this line ("market open", "autopilot not halted", "no
        # open position on X"). Shipped first as "{sym} is on hold",
        # which rendered as "✓ SENSEX is on hold (paused_symbols)" on
        # an APPROVED order — reading as held-and-approved-anyway. Same
        # class of dishonest label as the 2026-08-03 "stoploss" that was
        # really a profitable trail exit.
        check(not symbol_paused(job["symbol"], cfg),
              f"{job['symbol']} not on hold")
        # MarketSense risk gate (2026-08-08). marketsense_link.py's own
        # docstring says this gate "belongs in RiskAgent", and this is
        # the one function every order passes through, including manual
        # dashboard clicks via Orchestrator.manual_trade.
        #
        # THREE DELIBERATE LIMITS, because this hands a SEPARATE PROCESS
        # partial control over whether we trade:
        #   1. Only "hard_block" blocks. "penalty"/"suppressed" are
        #      advisory downgrades; vetoing on those would let a
        #      second-opinion service halt the book.
        #   2. The flag is honoured only while the LINK is fresh. The
        #      bridge deliberately keeps its last good values on the bus
        #      through an outage, so an unaged flag would block a symbol
        #      forever on a verdict withdrawn hours ago.
        #   3. It FAILS OPEN. If MarketSense is down or slow, trading
        #      continues and the skip is stated in the checks list —
        #      an optional advisory service must never be able to stop
        #      trading by falling over, the same rule llm.py follows.
        if cfg.get("marketsense_risk_gate_enabled", True):
            _ms = self.bus.get(f"ms_risk_flag:{job['symbol']}") or {}
            _link = self.bus.get("ms_link") or {}
            _age = time.time() - (_link.get("at_ts") or 0)
            _max_age = cfg.get("marketsense_max_flag_age_sec", 900)
            _fresh = bool(_link.get("ok")) and _age <= _max_age
            if _fresh:
                check(_ms.get("verdict") != "hard_block",
                      f"MarketSense: {job['symbol']} not hard-blocked")
            else:
                # Honest label: says the gate did NOT run. The 2026-08-03
                # lesson about "✓ SENSEX is on hold" applies — a tick
                # must never imply a check passed when it was skipped.
                check(True, f"MarketSense gate skipped — link stale "
                            f"({_age:.0f}s > {_max_age}s)")
        # PER-TRADE RUPEE CAP, EVALUATED HERE RATHER THAN AT EXECUTION.
        #
        # 2026-08-06. This ceiling already existed, but only inside
        # ExecutionAgent.place() — AFTER approval. The visible result
        # was a signal being APPROVED and then refused by the risk cap
        # a fraction of a second later, four times in 19 seconds:
        #
        #   11:18:05 BANKNIFTY 1 lot risks Rs 3,445 > cap Rs 2,000
        #   11:18:11 FINNIFTY  1 lot risks Rs 3,531 > cap Rs 2,000
        #   11:18:15 FINNIFTY  ... same
        #   11:18:22 FINNIFTY  ... same
        #
        # "APPROVED" for an order that was never placeable is a
        # misleading gate line, and each pass cost a full AI probability
        # evaluation plus a MEDIUM alert. The cap is NOT changed here
        # and is NOT loosened — config.py:152 states the invariant as
        # cap == risk_pct x capital and sizing.risk_coherence() confirms
        # it holds. This only moves WHERE it is evaluated, so a trade
        # that cannot be placed is rejected with a reason instead of
        # approved and then silently refused.
        #
        # NOTE, because it is a real consequence and not a side note: at
        # lots_per_trade=1 there is nothing to size down TO, so this can
        # only ever block (see place()'s own comment). At current lot
        # sizes and stop widths that closes BANKNIFTY and FINNIFTY
        # option buys entirely. That is a CALIBRATION question about
        # stop widths versus lot sizes, deliberately left open here —
        # moving the check must not quietly become a decision to widen
        # the cap.
        try:
            import sizing as _sz
            # ONE lot, deliberately. This gate answers exactly one
            # question: "can even a single lot fit inside the per-trade
            # rupee cap?" — because if not, the order is unplaceable and
            # should be rejected here rather than approved and refused a
            # fraction of a second later at execution.
            #
            # 2026-08-06 — it previously passed cfg["lots_per_trade"],
            # so the gate line read "capped 5->3 lot(s)" for a trade
            # that execution then filled at ONE lot. Execution sizes via
            # size_option_buy (dynamic sizing, deployed capital) and
            # caps THAT; the gate cannot know the answer and should not
            # print a number implying it does. Same class as the
            # "✓ SENSEX is on hold" label shipped and fixed earlier the
            # same day: a gate line stating something that is not true.
            _one_lot, _cap_why = _sz.cap_by_rupee_risk(
                cfg, job["symbol"], sig.get("entry"), sig.get("stoploss"),
                1, key="option_risk_per_trade_rupees")
            check(_one_lot >= 1,
                  "per-trade rupee cap allows at least 1 lot"
                  if _one_lot >= 1 else (_cap_why or "per-trade rupee cap"))
        except Exception as _e:
            # A failure to EVALUATE the cap must not silently approve.
            check(False, f"per-trade rupee cap could not be evaluated "
                         f"({type(_e).__name__})")
        # AI Decision Engine (Feature #8) — audit finding: Institutional
        # Activity (Feature #5) and Technical Confirmation (Feature #7)
        # were both fully built, tested, and displayed on the dashboard,
        # but neither was ever actually wired into trade approval —
        # every signal was scored purely on StrategyAgent's own OI-bias
        # confidence, contributing nothing from either confirmation
        # engine. Fixed here: pulls both engines' CURRENT output for
        # this symbol (already computed every TechnicalAgent cycle, no
        # new calculation) and adjusts sig["confidence"] up/down based
        # on agreement — matching Feature #7's own explicit design
        # philosophy ("technical indicators only increase or decrease
        # confidence... never generate signals alone"), just never
        # connected until now. The min_confidence check right after
        # this uses the ADJUSTED value, so agreement/disagreement can
        # genuinely push a borderline signal over or under the bar.
        if cfg.get("ai_decision_engine_enabled", True):
            import ai_decision_engine as ade
            institutional = self.bus.get(f"institutional:{job['symbol']}")
            technical = self.bus.get(f"technical:{job['symbol']}")
            # v58 — captured BEFORE the Decision Engine overwrites
            # sig["confidence"] below, so the Unified Probability stage
            # can use the option-chain's own PRE-adjustment confidence
            # as one distinct input, alongside the POST-adjustment one.
            sig["_pre_decision_confidence"] = sig.get("confidence")
            decision = ade.evaluate_signal(sig, institutional, technical)
            sig["confidence"] = decision["adjusted_confidence"]
            sig["ai_decision_notes"] = decision["notes"]
            sig["ai_decision"] = decision   # structured form — avoids
            # downstream code needing to string-parse the notes list to
            # find out what institutional_agreement/technical_agreement
            # were (used by the position dict at entry time, below).
            check(not decision["hard_block"],
                 decision["hard_block_reason"] or
                 "AI Decision Engine: institutional/technical both conflict")
            # AI Probability Engine (Feature #8) — distinct from the
            # Decision Engine above: an EMPIRICAL win-probability
            # estimate from this system's own historical trade record
            # (bucketed by confidence/institutional-agreement/regime),
            # not another real-time heuristic. Attached to the signal
            # for display/audit, NOT used as a gate here — with sample
            # sizes this small early on, gating on it would be
            # premature; the honest "low confidence in estimate" flag
            # is the signal a human should weigh, not an automatic
            # block. (A future toggle could gate on it once enough
            # history accumulates — not added yet, matching this
            # engine's own "don't fabricate certainty from small
            # samples" principle.)
            import ai_probability_engine as ape
            regime = self.bus.get(f"regime:{job['symbol']}")
            closed_trades = self.bus.get("closed_trades", [])
            probability = ape.estimate_probability(sig, decision, regime, closed_trades)
            sig["ai_probability"] = probability
            if not probability["unavailable"]:
                check(True, f"AI Probability: {probability['probability_pct']}% "
                           f"({probability['confidence_in_estimate']} confidence, "
                           f"n={probability['sample_size']})")
            # v58 — Unified AI Probability stage: one combined number
            # from all four upstream engines (Option Chain/Decision
            # Engine/Institutional/Technical) plus the empirical
            # estimate above, rather than a human having to weigh five
            # separate scores themselves. Advisory only, same reasoning
            # as the plain probability estimate above — not gated on,
            # attached for display/audit.
            unified = ape.unified_probability(sig, decision, probability,
                                              institutional, technical)
            sig["unified_probability"] = unified
            if not unified["unavailable"]:
                check(True, f"Unified AI Probability: "
                           f"{unified['unified_probability_pct']}% "
                           f"({unified['basis']})")
            # AI Learning Engine feedback loop (Feature #8) — per
            # explicit request to make the daily journal actually feed
            # BACK into signal generation. LearningAgent flags named
            # underperforming patterns (confidence bucket ×
            # institutional agreement × regime, win rate <35% with
            # n>=5) each day; this checks whether TODAY's signal
            # matches one of those flagged patterns and applies an
            # extra confidence penalty if so — a soft, visible
            # adjustment (not a hard block), matching this whole
            # feature's own "small-sample honesty, human stays in the
            # loop" principle rather than silently vetoing trades.
            if cfg.get("learning_feedback_enabled", True):
                flagged = self.bus.get("learned_underperforming_patterns", [])
                sig_bucket = ape._bucket_confidence(sig["confidence"])
                sig_regime = (regime or {}).get("regime")
                match = next((f for f in flagged if
                            f["confidence_bucket"] == sig_bucket and
                            f["institutional_agreement"] == decision["institutional_agreement"] and
                            f["regime"] == sig_regime), None)
                if match:
                    penalty = 15
                    sig["confidence"] = max(0, sig["confidence"] - penalty)
                    check(True, f"⚠ matches an underperforming pattern from the "
                               f"learning journal ({match['win_rate']}% win rate, "
                               f"n={match['sample_size']}) — confidence -{penalty}")
        # v59.0 item 9 — basis residual gate. DEFAULT OFF, its own change,
        # separate from Phase D. A VETO only: `basis_residual.agrees()`
        # returns True whenever it has no opinion (no z yet, or inside the
        # band), so this can never be the reason a trade happens — only a
        # reason one does not. It sits alongside the existing gates rather
        # than bypassing any of them.
        if True:
            import basis_residual as _br
            _obs = self.bus.get(f"basis_residual:{job.get('symbol')}") or {}
            _side = "LONG" if (sig.get("type") or "").upper().startswith("C") \
                else ("SHORT" if (sig.get("type") or "").upper().startswith("P")
                      else None)
            _skey = (sig.get("strategy") or "signal").lower()
            _ok, _why = _br.gate_for(_skey, _side, _obs.get("residual_z"), cfg)
            if not _ok:
                check(False, _why)
            elif _obs.get("residual_z") is not None and cfg.get(
                    f"{_skey}_require_basis_agreement",
                    cfg.get("require_basis_agreement", False)):
                check(True, _why)
        check(sig["confidence"] >= cfg["min_confidence"],
              f"confidence {sig['confidence']}≥{cfg['min_confidence']}")
        check(sig.get("entry", 0) > 0 and sig.get("stoploss", 0) > 0,
              "valid price points")
        entry, sl, t1 = sig.get("entry", 0), sig.get("stoploss", 0), sig.get("target1", 0)
        rr = (t1 - entry) / (entry - sl) if entry > sl else 0
        check(rr >= 1.95, f"risk-reward {rr:.1f} (need ≥2.0)")
        atm = job["analysis"].get("atm")
        strike = sig.get("strike")
        # 2026-07-29 — this gate rejected 8 signals/day with the message
        # "strike 24100 not OTM (ATM 24200)". That message was WRONG:
        # for a PUT, 24100 against a 24200 spot IS out-of-the-money. The
        # condition (`strike >= atm` for PE, `strike <= atm` for CE)
        # actually enforces AT- or IN-the-money — the sane reading being
        # "don't buy far-OTM lottery tickets with no delta". So the
        # policy may well be right while the label was the opposite of
        # what it did, which made every one of those rejections
        # unreadable.
        #
        # The condition is NOT silently flipped here. This is a live
        # trading gate and guessing at intent is how real money is lost;
        # the policy is made explicit and configurable instead, DEFAULT
        # UNCHANGED, and the message now states what it enforces.
        policy = cfg.get("option_strike_policy", "atm_or_itm")
        if strike is None or not atm:
            check(False, "strike/ATM unavailable")
        elif policy == "any":
            check(True, f"strike {strike} (policy: any)")
        elif sig.get("signal") == "BUY_CE":
            ok_itm = strike <= atm
            check(ok_itm if policy == "atm_or_itm" else strike >= atm,
                  (f"CE strike {strike} vs ATM {atm} — policy "
                   f"'{policy}' wants " +
                   ("at-or-in-the-money (strike <= ATM)" if policy == "atm_or_itm"
                    else "at-or-out-of-the-money (strike >= ATM)")))
        elif sig.get("signal") == "BUY_PE":
            ok_itm = strike >= atm
            check(ok_itm if policy == "atm_or_itm" else strike <= atm,
                  (f"PE strike {strike} vs ATM {atm} — policy "
                   f"'{policy}' wants " +
                   ("at-or-in-the-money (strike >= ATM)" if policy == "atm_or_itm"
                    else "at-or-out-of-the-money (strike <= ATM)")))

        # ---- Regime & multi-timeframe checks (from RegimeAgent) ----
        # This is what would have blocked last Monday's 4 losing BUY_CE
        # trades in a rangebound/choppy tape.
        if cfg.get("regime_gate_enabled", True):
            regime = self.bus.get(f"regime:{job['symbol']}") or {}
            regime_session = regime.get("session_date")
            if regime and regime_session and \
                    regime_session != now_ist().strftime("%Y-%m-%d"):
                # 2026-07-26 — belt-and-braces, and it also closes a
                # PRE-EXISTING hole: RegimeAgent's last successful cycle
                # of the previous session leaves its result sitting in
                # this key overnight. At the next day's open, before the
                # first fresh cycle completes (~90s), the gate was
                # happily approving or rejecting signals on YESTERDAY's
                # trend and confluence. Keyed on session_date rather
                # than the `stale` flag so it catches that lingering
                # case too, not just the deliberate last-session reads.
                check(True, f"regime data for {job['symbol']} is from "
                            f"{regime_session}, not today — not blocking, "
                            f"awaiting today's first read")
            elif not regime:
                # No regime data yet for this symbol (warmup / just switched)
                # — don't block on missing data, just note it.
                check(True, f"regime data pending for {job['symbol']} (not blocking)")
            else:
                reg_label = regime.get("regime", "unknown")
                allowed = regime.get("allowed_signals", [])
                if allowed:
                    check(sig["signal"] in allowed,
                          f"regime '{reg_label}' allows {allowed or 'nothing'}")
                elif reg_label in ("choppy", "rangebound"):
                    # explicit block for known-bad regimes
                    check(False, f"regime is {reg_label} — avoid directional buys")
                # multi-timeframe confluence — only when we actually have data
                confluence = regime.get("confluence", "no-alignment")
                if cfg.get("require_tf_confluence", True):
                    if sig["signal"] == "BUY_CE":
                        check(confluence in ("strong-bull", "mixed-bull"),
                              f"timeframe confluence for CE ({confluence})")
                    elif sig["signal"] == "BUY_PE":
                        check(confluence in ("strong-bear", "mixed-bear"),
                              f"timeframe confluence for PE ({confluence})")
        # News risk/opportunity scoring (roadmap: replaces the old blanket
        # block window). Only blocks trades whose direction actually
        # CONFLICTS with the news; an aligned direction is treated as an
        # opportunity and is never blocked, and the effect decays over
        # `news_block_minutes` instead of a hard on/off cliff.
        news = self.bus.get("news") or {}
        news_block, news_note, news_score = news_risk_opportunity(
            news, sig.get("signal"), cfg)
        self.bus.set(f"news_score:{job['symbol']}", news_score)
        check(not news_block, news_note)
        max_loss = (sig.get("entry", 0) - sig.get("stoploss", 0)) \
            * cfg["lot_sizes"].get(job["symbol"], 75) * cfg["lots_per_trade"]
        check(self.daily_pnl - max_loss > -abs(cfg.get("daily_loss_limit", 5000)),
              f"daily loss limit (risking ₹{max_loss:.0f}, day P&L ₹{self.daily_pnl:.0f})")
        # v59.73 (third-eye Tier 2) — structural feasibility: the trade's
        # own FIRST target must clear min_edge_cost_ratio × the modelled
        # round trip. The August record grossed ₹158/trade against ₹176
        # of friction — trades designed below their own costs are not
        # marginal, they are structurally impossible, and no downstream
        # tuning fixes that.
        import edge_feasibility
        _edge_ok, _edge_detail = edge_feasibility.option_buy_feasible(
            sig.get("entry", 0), sig.get("target1", 0),
            cfg["lot_sizes"].get(job["symbol"], 75), cfg)
        check(_edge_ok, _edge_detail)
        profit_target = cfg.get("daily_profit_target", 0)
        if profit_target > 0:
            # Bug found 2026-07-24 from live logs: check() logs its label
            # on EVERY call (prefixed ✓/✗), not just on failure — this
            # message was hardcoded to always read as the failure case
            # ("reached... locking in"), so a normal PASSING check (day
            # P&L still under target) printed the nonsensical "✓ daily
            # profit target reached (₹0 ≥ ₹50000)" — literally false
            # arithmetic shown as if it were true. Message now correctly
            # describes whichever state actually holds.
            under_target = self.daily_pnl < profit_target
            check(under_target,
                  (f"daily profit target not yet reached (₹{self.daily_pnl:.0f} < "
                   f"₹{profit_target:.0f})") if under_target else
                  (f"daily profit target reached (₹{self.daily_pnl:.0f} ≥ "
                   f"₹{profit_target:.0f}) — locking in today's gain, no new positions"))
        data_age, age_why = data_age_of(self.bus, f"chain_ts:{job['symbol']}",
                                        "chain_ts",
                                        label=f"{job['symbol']} chain data")
        # Missing data must SKIP, never reject — the project's standing
        # graceful-degradation rule. Only a genuinely stale feed fails.
        if data_age is None:
            check(True, f"{age_why} — not blocking")
        else:
            check(data_age < 30, f"fresh {job['symbol']} data ({data_age:.0f}s old)")
        return ok, checks

    def _oi_composite_observe(self, cfg):
        """Evaluate Strategy 10 every cycle and LOG it, trade only if
        auto_deploy is on.

        Observation is unconditional by design. The lesson from S8 --
        which sat unevaluated for nine versions because its auto_deploy
        gate came before its evaluation -- is that a gate must govern
        TRADING, never observation. This strategy is brand new and
        entirely unvalidated, so the first thing it must do is show its
        working on real chains.
        """
        import oi_composite as oic
        p = {k[len("oi_composite_"):]: v for k, v in cfg.items()
             if k.startswith("oi_composite_")}
        if not p.get("enabled", True):
            return
        # 2026-07-31 — was `self.symbols`, which no agent has ever defined:
        # the Agent base sets only bus/ctx/stop_evt/last_run/status/summary.
        # Every cycle since v58.65 raised AttributeError here, so S10 -- the
        # strategy shipped observe-only precisely so it would "show its
        # working on real chains" -- observed nothing at all. Its 72 tests
        # passed throughout because they call oi_composite.detect_setup()
        # directly and never reach this call site. Same bus key the other
        # agents read (Orchestrator.start sets it).
        for sym in self.bus.get("symbols", ["NIFTY"]):
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis:
                continue
            fq = self.bus.get(f"future_oi_quadrant:{sym}")
            try:
                setup, detail = oic.detect_setup(analysis, fq, p)
            except Exception as e:
                self.bus.log(self.name, f"{sym}: S10 detect FAILED "
                                        f"({type(e).__name__}: {e})")
                continue
            self.bus.set(f"oi_composite:{sym}",
                         {"setup": setup, "detail": detail})
            if not setup:
                continue
            lot = cfg["lot_sizes"].get(sym, 75)
            cap = cfg.get("backtest_capital", 1000000)
            lots, si = oic.size_composite(setup, analysis, cap, lot, p)
            self.bus.log(self.name,
                         f"{sym}: S10 {setup['kind']} — {detail.get('why', '')} "
                         f"| {lots} lot(s): {si.get('why', '')}")
            if not p.get("auto_deploy", False):
                continue      # observed and published above; cannot trade
            self.bus.log(self.name, f"{sym}: S10 auto_deploy is ON but the "
                                    "composite executor is not built yet — "
                                    "observation only")

    def cycle(self):
        try:
            self._oi_composite_observe(config.load())
        except Exception as e:
            self.bus.log(self.name, f"S10 observe cycle FAILED "
                                    f"({type(e).__name__}: {e})")
        if time.time() - getattr(self, "_last_shadow_resolve", 0) > 30:
            self._last_shadow_resolve = time.time()
            try:
                _resolve_shadow_signals(self.bus, self.ctx.get("get_chain"))
            except Exception:
                pass
        # AI Risk Score (Feature #9) — ambient, portfolio-wide, refreshed
        # independently of whether there's a signal to evaluate right
        # now (unlike evaluate()'s per-signal Decision/Probability
        # Engine calls above). Throttled to every 10s since it aggregates
        # across potentially several open positions' live chain data —
        # no need to recompute on every 5s cycle tick.
        if time.time() - getattr(self, "_last_risk_score", 0) > 10:
            self._last_risk_score = time.time()
            try:
                import risk_engine as rengine
                import sizing
                cfg = config.load()
                positions = self.bus.get("positions", {}) or {}
                spreads = self.bus.get("spreads", {}) or {}
                closed_trades = self.bus.get("closed_trades", [])
                deployed = sizing.deployed_capital(cfg, positions, spreads)
                get_chain_fn = self.ctx.get("get_chain")
                # Ambient volatility/institutional/technical reads use
                # whichever symbol currently has the most attention —
                # the displayed/most-recently-analyzed symbol — since
                # the portfolio-wide score needs SOME representative
                # market-condition reading, not one per open position.
                ref_symbol = next(iter(positions), None) or self.bus.get("current_symbol") or "NIFTY"
                analysis = self.bus.get(f"analysis:{ref_symbol}")
                institutional = self.bus.get(f"institutional:{ref_symbol}")
                technical = self.bus.get(f"technical:{ref_symbol}")
                score = rengine.compute_ai_risk_score(
                    daily_pnl=self.daily_pnl,
                    daily_loss_limit=cfg.get("daily_loss_limit", 5000),
                    position_count=len(positions),
                    max_positions=cfg.get("max_concurrent_positions", 1),
                    positions=positions, get_chain_fn=get_chain_fn or (lambda s: None),
                    deployed_capital=deployed,
                    market_risk_meter=(analysis or {}).get("risk_meter"),
                    institutional_score=(institutional or {}).get("institutional_score"),
                    technical_volatility_pct=(technical or {}).get("volatility_pct"),
                    news_score=self.bus.get(f"news_score:{ref_symbol}"),
                    spreads=spreads)
                portfolio_greeks = rengine.aggregate_portfolio_greeks(
                    positions, get_chain_fn or (lambda s: None), spreads=spreads)
                self.bus.set("ai_risk_score", score)
                self.bus.set("portfolio_greeks", portfolio_greeks)

                # Live Portfolio Monitor (Feature #9) — Expected Loss +
                # rolling time-series for drawdown/trend display, per
                # the spec's own "Monitor: Open PNL, Risk, Margin,
                # Greeks, Exposure, Drawdown, Expected Loss,
                # Probability, Confidence" requirement. Reuses the AI
                # Probability Engine directly (not a new statistical
                # model) — for each open position, estimates a CURRENT
                # win probability from its own entry-time profile
                # matched against everything learned since (the SAME
                # bucketing ai_probability_engine already does),
                # answering "given what we knew at entry, and
                # everything learned since, what's our best estimate
                # now."
                import ai_probability_engine as ape
                probabilities_by_symbol = {}
                for sym, pos in positions.items():
                    synthetic_sig = {"confidence": pos.get("entry_confidence")}
                    synthetic_decision = {
                        "institutional_agreement": pos.get("entry_institutional_agreement"),
                        "technical_agreement": pos.get("entry_technical_agreement")}
                    synthetic_regime = {"regime": pos.get("entry_regime")}
                    probabilities_by_symbol[sym] = ape.estimate_probability(
                        synthetic_sig, synthetic_decision, synthetic_regime, closed_trades)
                exp_loss = rengine.expected_loss(positions, probabilities_by_symbol)
                self.bus.set("expected_loss", exp_loss)

                # Rolling history for trend display — peak-tracked
                # drawdown (today's best daily_pnl seen so far, vs now)
                # is a genuinely different number from the portfolio
                # kill-switch's own real-time UNREALIZED check
                # (ExecutionAgent._check_portfolio_kill_switch) — this
                # is a REALIZED-P&L peak-to-current measure for display/
                # trend purposes, not a second copy of the kill-switch's
                # own trigger logic.
                # v59.69 — refresh from the shared definition, and roll
                # the peak tracker over at date change (it used to carry
                # yesterday's peak into today, making the first hours of
                # a session look like a drawdown from a phantom high).
                self.daily_pnl = realized_pnl_today(self.bus)
                _today = now_ist().strftime("%Y-%m-%d")
                if getattr(self, "_peak_date", None) != _today:
                    self._peak_date = _today
                    self._peak_daily_pnl = 0.0
                self._peak_daily_pnl = max(getattr(self, "_peak_daily_pnl", 0.0), self.daily_pnl)
                drawdown = self._peak_daily_pnl - self.daily_pnl
                history_point = {"ts": time.time(), "daily_pnl": self.daily_pnl,
                                 "drawdown": drawdown, "risk_score": score.get("score"),
                                 "expected_loss": exp_loss.get("total_expected_loss")}
                risk_history = self.bus.get("risk_history", [])
                risk_history.append(history_point)
                self.bus.set("risk_history", risk_history[-500:])   # capped rolling window
            except Exception as e:
                self.bus.log(self.name, f"⚠ AI Risk Score computation failed: {e}")
        if not self._queue:
            return
        job = self._queue.popleft()
        ok, checks = self.evaluate(job)
        sig = job["signal"]
        verdict = "APPROVED" if ok else "REJECTED"
        self.summary = f"{verdict}: {sig['signal']} {sig.get('strike','')}"
        trade_params = (f"entry ₹{sig.get('entry')} SL ₹{sig.get('stoploss')} "
                       f"T1 ₹{sig.get('target1')} T2 ₹{sig.get('target2')}")
        self.bus.log(self.name, f"{verdict} — " + " · ".join(checks) + f" · {trade_params}")
        self.bus.set("last_risk_check", {"verdict": verdict, "checks": checks})
        _log_shadow_signal(self.bus, job, verdict, checks)
        # DB persistence (Feature #9) — per the spec's own explicit
        # "Store Risk Score, Trade Quality, Liquidity Score, Greeks,
        # Exposure, Reason, Approval, Rejection, Timestamp" requirement.
        # EVERY decision, not just approvals, so a later review can see
        # what got turned away and why. Reuses data already computed
        # this cycle (sig["ai_probability"], the ambient ai_risk_score/
        # portfolio_greeks bus keys from earlier this cycle) rather
        # than recomputing anything.
        try:
            import history
            import risk_engine as rengine
            cfg = config.load()
            atm = job.get("analysis", {}).get("atm")
            spot = job.get("analysis", {}).get("spot")
            get_chain_fn = self.ctx.get("get_chain") or (lambda s: None)
            chain = get_chain_fn(job["symbol"])
            chain_row = next((r for r in (chain or {}).get("rows", [])
                             if r.get("strike") == sig.get("strike")), None)
            leg = "ce" if sig.get("signal") == "BUY_CE" else "pe"
            sq = rengine.strike_quality(chain_row, leg, atm, sig.get("strike"), spot)
            tq = rengine.trade_quality_score(
                institutional=self.bus.get(f"institutional:{job['symbol']}"),
                technical=self.bus.get(f"technical:{job['symbol']}"),
                chain_row=chain_row, leg=leg, atm=atm, strike=sig.get("strike"),
                spot=spot, strike_interval=50,
                probability=sig.get("ai_probability"),
                regime=self.bus.get(f"regime:{job['symbol']}"))
            ambient_score = self.bus.get("ai_risk_score") or {}
            ambient_greeks = self.bus.get("portfolio_greeks")
            history.insert_risk_decision(
                ts=time.time(), symbol=job["symbol"], signal=sig.get("signal"),
                verdict=verdict, risk_score=ambient_score.get("score"),
                risk_level=ambient_score.get("risk_level"),
                trade_quality_score=tq.get("score"),
                liquidity_score=sq.get("score"),
                portfolio_greeks=ambient_greeks,
                deployed_capital=None,
                reason="; ".join(c for c in checks if c.startswith("✗")) or "all checks passed")
        except Exception as e:
            self.bus.log(self.name, f"⚠ failed to persist risk decision: {e}")
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
        # 2026-08-06 — see place()'s docstring. Symbols with an entry
        # IN FLIGHT but not yet registered in bus["positions"].
        # manual_trade() runs on the HTTP thread while cycle() runs on
        # the agent thread, so this genuinely needs a lock.
        self._entering = set()
        self._entry_lock = threading.Lock()

    def cycle(self):
        # v59.71 (third-eye Tier 4) — per-step isolation: one
        # deterministic exception in _monitor() used to skip spread/
        # futures monitoring and auto-deploy every cycle, forever.
        #
        # v59.72 (R2 finding H1) — isolation is right for MONITORS and
        # wrong for the GUARD. Isolating the kill-switch meant a
        # crashing guard logged a line while the cycle went on to drain
        # the entry queue and auto-deploy — the exact behaviour the old
        # bare sequence prevented by accident. A guard that cannot run
        # must halt what it guards: on kill-switch failure the entry-
        # generating steps are skipped this cycle AND the shared halt
        # flag is raised so RiskAgent/enter_future/enter_spread refuse
        # too. Exits and monitors still run — closing positions stays
        # safe when the guard is broken; opening new ones does not.
        guard_err = None
        try:
            self._check_portfolio_kill_switch()
        except Exception as e:
            guard_err = e
            self.bus.log(self.name,
                         f"⚠ step _check_portfolio_kill_switch failed: "
                         f"{type(e).__name__}: {e} — entries halted")
            self.bus.set("portfolio_halt_until", time.time() + 120)
            if time.time() - getattr(self, "_guard_alert_ts", 0) > 300:
                self._guard_alert_ts = time.time()
                self.bus.alert("high", self.name, "PORTFOLIO",
                               f"kill-switch UNAVAILABLE "
                               f"({type(e).__name__}: {e}) — new entries "
                               f"halted until it runs again")
        steps = [self._reconcile_broker,     # v59.69 — live-only, throttled
                 self._order_ws_manage]      # v59.76 — live-only lifecycle
        if guard_err is None:
            steps.append(self._drain_entry_queue)
        steps += [self._monitor,
                  self._monitor_spreads,
                  self._monitor_futures]     # S4 (v50)
        if guard_err is None:
            steps += [self._futures_signal_engine,  # S4 Phase 2 (v52)
                      self._auto_spreads]
        first_err = None
        for step in steps:
            try:
                step()
            except Exception as e:
                self.bus.log(self.name,
                             f"⚠ step {step.__name__} failed: "
                             f"{type(e).__name__}: {e}")
                if first_err is None:
                    first_err = e
        if guard_err is not None:
            raise guard_err
        if first_err is not None:
            raise first_err

    def _drain_entry_queue(self):
        if self._queue:
            self._enter(self._queue.popleft())

    # ------------------------------------------------------------------
    # S4 — FUTURES TRADING (v50, Phase 1: paper-only, manual/API driven)
    #
    # Scope decision (the question left open across sessions): this
    # implements real futures TRADING as a new position type — its own
    # margin accounting, direction-aware P&L, SL/target/trailing, fees,
    # EOD square-off, and inclusion in the portfolio kill-switch — NOT
    # merely richer futures data (that already exists via future_ohlc/
    # future_oi_trend). Phase 1 is deliberately paper-only and manual
    # (dashboard buttons / API): the entry-signal engine for futures is
    # Phase 2, after this position machinery has been proven the same
    # paper-first way every options strategy was.
    #
    # Data source: future_ohlc:{sym}.close — the SAME futures feed the
    # LTP Monitor panel and OI-buildup logic already consume (websocket
    # ticks + the REST quote poll), no new subscription.
    # ------------------------------------------------------------------

    def enter_future(self, symbol, side, lots=1, order_type="MARKET"):
        """Open a futures position (paper, or live if explicitly enabled).
        side: LONG or SHORT.

        2026-07-26 (v52, S4 Phase 2) — live orders added behind TWO
        independent switches, not one: cfg["paper_mode"] (the account-
        wide switch every strategy already respects) AND
        futures_live_enabled (a SEPARATE, futures-specific switch,
        default off). A futures contract has materially different risk
        than an option buy — unlimited-ish downside on the wrong side,
        no premium ceiling — so it gets its own explicit opt-in rather
        than inheriting live status automatically the moment paper_mode
        is turned off for options. Both must be true for a real order.
        """
        cfg = config.load()
        side = str(side).upper()
        if side not in ("LONG", "SHORT"):
            return {"error": f"side must be LONG or SHORT, got {side!r}"}
        if symbol_paused(symbol, cfg):
            return {"error": f"{symbol} is on hold (paused_symbols)"}
        live = not cfg.get("paper_mode", True)
        if live and not cfg.get("futures_live_enabled", False):
            return {"error": "live futures trading needs BOTH paper_mode "
                             "off AND futures_live_enabled on — the second "
                             "switch is deliberate, futures risk isn't "
                             "capped like an option premium is"}
        if not market_open():
            return {"error": "market is closed"}
        if time.time() < self.bus.get("portfolio_halt_until", 0):
            return {"error": "portfolio kill-switch cooldown active"}
        # v59.69 (third-eye Tier 3) — the DAILY LOSS GATE, here. Futures
        # entries never traverse RiskAgent.evaluate(), so on 2026-07-30
        # futures kept opening all afternoon while the options path was
        # correctly blocked — 19 futures trades lost ₹73,115 against a
        # daily limit the gate never saw. Week 31's own journal showed
        # the asymmetry: 357 risk decisions, 19 approvals, 129 closed
        # trades. Same shared figure the options gate uses
        # (realized_pnl_today), risked amount = the per-trade rupee cap
        # this function already enforces below.
        _day = realized_pnl_today(self.bus)
        _risk_cap = float(cfg.get("futures_risk_per_trade_rupees", 2500))
        _limit = abs(cfg.get("daily_loss_limit", 5000))
        if _day - _risk_cap <= -_limit:
            return {"error": f"daily loss limit: day P&L ₹{_day:.0f}, "
                             f"risking ₹{_risk_cap:.0f} more would breach "
                             f"-₹{_limit:.0f} — no futures entries for the "
                             f"rest of the day"}
        futs = self.bus.get("futures_positions", {}) or {}
        if symbol in futs:
            return {"error": f"{symbol} already has an open futures "
                             f"position — exit it first"}
        ohlc = self.bus.get(f"future_ohlc:{symbol}") or {}
        ltp = ohlc.get("close")
        if not ltp:
            return {"error": f"no live futures price for {symbol} yet "
                             f"(future_ohlc empty — feed warming up or "
                             f"rate-limited)"}
        lots = max(1, int(lots))
        lot_size = cfg["lot_sizes"].get(symbol, 75)

        # ---------------------------------------------------------------
        # 2026-08-01 — PER-TRADE RUPEE RISK CAP, applied here because this
        # is the one function every futures entry passes through.
        #
        # `futures_risk_per_trade_rupees` was already set to ₹2,500 and
        # sizing.cap_by_rupee_risk() already existed, but the cap lived in
        # sizing.size_future() — one of THREE entry paths. The auto-deploy
        # and manual_deploy paths size through it; /api/futures/enter takes
        # `body.lots` straight from the request and never did. On
        # 2026-07-30 that produced, against a ₹20,000 daily loss limit:
        #
        #     BANKNIFTY  8 lots   -₹18,240   (91% of the DAILY limit, one trade)
        #     FINNIFTY   4 lots   -₹15,840   (79%)
        #     FINNIFTY   6 lots   -₹12,840   (64%)
        #
        # 19 futures trades lost ₹73,115 that day while the spread book
        # made ₹1,202. A limit one trade can consume 91% of is not a limit.
        #
        # The stop had to move above the order placement to do this: it
        # was computed ~40 lines BELOW, so the size could never be checked
        # against the risk it implied until after the order existed.
        # Sizing a position you have already bought is not sizing.
        # ---------------------------------------------------------------
        sl_pct = cfg.get("futures_sl_pct", 0.4)
        tgt_pct = cfg.get("futures_target_pct", 0.8)
        sign = 1 if side == "LONG" else -1
        # v58.39 — ATR geometry, same source as the entry gate above so
        # the stop the signal was sized against is the stop the position
        # actually gets. Target is a MULTIPLE OF THE STOP, which keeps
        # the designed payoff ratio intact across volatility regimes;
        # the old fixed 0.4%/0.8% pair produced a realised payoff of
        # 0.77 against a designed 2.0 because no trade ever reached it.
        import sizing as _sz
        _atr = None
        if cfg.get("futures_stop_mode", "atr") == "atr":
            _pk = self.bus.get(f"pa_candles:{symbol}") or {}
            _atr = _sz.atr_points(_pk.get("c5") or [],
                                  cfg.get("futures_atr_period", 14))
        if _atr:
            _stop_pts = _atr * cfg.get("futures_atr_stop_mult", 1.5)
            _tgt_pts = _atr * cfg.get("futures_atr_target_mult", 2.75)
            _sl_px = round(ltp - sign * _stop_pts, 2)
            _tg_px = round(ltp + sign * _tgt_pts, 2)
        else:
            _sl_px = round(ltp * (1 - sign * sl_pct / 100), 2)
            _tg_px = round(ltp * (1 + sign * tgt_pct / 100), 2)

        # v59.73 (third-eye Tier 2) — structural feasibility: the target
        # this entry is designed around must clear min_edge_cost_ratio ×
        # the notional round trip, or the trade cannot be net-positive
        # even when it WINS as designed.
        import edge_feasibility
        _edge_ok, _edge_detail = edge_feasibility.future_feasible(
            symbol, ltp, _tg_px, lot_size, lots, cfg)
        if not _edge_ok:
            self.bus.log(self.name,
                         f"⛔ {symbol} FUT {side} REFUSED — {_edge_detail}")
            return {"error": f"edge below cost: {_edge_detail}"}

        _capped, _cap_why = _sz.cap_by_rupee_risk(cfg, symbol, ltp, _sl_px, lots)
        if _capped < lots:
            _risk_per_lot = abs(ltp - _sl_px) * lot_size
            if _capped <= 0:
                # Refusing is the correct answer: at this stop distance even
                # one lot breaches the cap. Silently taking it anyway is how
                # a ₹2,500 ceiling produced an ₹18,240 loss.
                self.bus.log(self.name,
                             f"⛔ {symbol} FUT {side} REFUSED — 1 lot risks "
                             f"₹{_risk_per_lot:,.0f} (stop {abs(ltp - _sl_px):.1f} "
                             f"pts × {lot_size}), over the ₹"
                             f"{cfg.get('futures_risk_per_trade_rupees', 0):,.0f} "
                             f"per-trade cap. {_cap_why}")
                return {"error": f"per-trade risk cap: 1 lot would risk "
                                 f"₹{_risk_per_lot:,.0f} against a ₹"
                                 f"{cfg.get('futures_risk_per_trade_rupees', 0):,.0f} "
                                 f"cap — no size is permissible at this stop "
                                 f"distance"}
            self.bus.log(self.name,
                         f"{symbol} FUT {side} sized DOWN {lots}→{_capped} lot(s) "
                         f"by the ₹{cfg.get('futures_risk_per_trade_rupees', 0):,.0f} "
                         f"per-trade cap (₹{_risk_per_lot:,.0f} risk/lot). {_cap_why}")
            lots = _capped

        margin_per_lot = cfg.get("margin_per_lot_future", 110000)
        # 2026-07-26 (v52) — margin-aware against REAL deployed capital.
        # Phase 1 read a "capital_deployed" bus key that nothing in the
        # codebase ever wrote (confirmed by search), so this gate always
        # compared against 0 regardless of what else was open — a real
        # bug, found while building Phase 2's sizing work. Now routes
        # through the same sizing.deployed_capital() options and
        # spreads already use, extended to include open futures margin.
        import sizing
        capital = cfg.get("backtest_capital", 100000)
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        deployed = sizing.deployed_capital(cfg, positions, spreads, futs)
        need = margin_per_lot * lots
        if need > max(0, capital - deployed):
            return {"error": f"insufficient margin: need \u20b9{need:,.0f} "
                             f"({lots} lot(s) \u00d7 \u20b9{margin_per_lot:,.0f}), "
                             f"available \u20b9{max(0, capital - deployed):,.0f} "
                             f"(\u20b9{deployed:,.0f} already deployed across "
                             f"positions/spreads/futures)"}
        order_id = paper_order_id()
        if live:
            # Live path: real order on the FRONT-month contract's own
            # security_id (future_months:{sym}["front"] — populated by
            # MarketDataAgent's existing monthly-rollover subscription,
            # no new lookup needed). Uses the SAME orders_factory() the
            # options live path already goes through.
            orders = self.ctx.get("orders_factory", lambda: None)()
            front = (self.bus.get(f"future_months:{symbol}") or {}).get("front")
            if orders is None or not front or not front.get("security_id"):
                return {"error": "cannot place live futures order — no "
                                 "broker / front-month security_id "
                                 "resolved yet (future_months empty)"}
            resp = orders.place(symbol, front["security_id"],
                                "BUY" if side == "LONG" else "SELL",
                                lots * lot_size, order_type)
            order_id = resp.get("orderId") or "UNCONFIRMED"
            self.bus.log(self.name, f"\U0001f534 LIVE FUT {side} {symbol} "
                         f"\u00d7{lots} lot(s) @ {ltp} \u2014 order {order_id}")
            _st = self._confirm_order(orders, resp, f"FUT {side} {symbol}")
            # v59.72 (R2 finding H2) \u2014 a rejected futures BUY/SELL means
            # no fill; do not build a position for it.
            if _st in ("REJECTED", "CANCELLED"):
                return {"error": f"live futures order {_st} at the broker "
                                 f"\u2014 nothing tracked (see alert)"}
            # v59.75 — real fill for the entry price; stops/targets stay
            # as designed at the quote (the geometry the sizing was
            # approved against), the ENTRY price is what actually traded.
            _fpx, _ = self._actual_fill(orders, resp, lots * lot_size,
                                        f"FUT {side} {symbol}")
            if _fpx:
                _quote_at_entry = ltp
                ltp = _fpx
        # (stop/target geometry and the rupee cap were computed above,
        # BEFORE the order was placed — see the block after `lot_size`.
        # _sl_px/_tg_px are already bound here.)
        # v58.49 (roadmap B1) — futures had NO chart markers while
        # options and S7 both did, so the one instrument class that was
        # losing money was also the only one you could not see on the
        # chart. Uses the INDEX spot, not the futures price: the chart
        # plots the index, and a futures contract trades at a basis to
        # it, so marking the futures LTP would place the marker at a
        # price the plotted series never touched.
        _idx_spot = (self.bus.get(f"analysis:{symbol}") or {}).get("spot")
        self._record_chart_event(symbol, "entry", _idx_spot or None,
                                 f"FUT {side} x{lots}")
        pos = {"symbol": symbol, "kind": "future", "side": side,
               "lots": lots, "lot_size": lot_size, "entry": ltp,
               "sl": _sl_px, "target": _tg_px,
               "atr_at_entry": round(_atr, 1) if _atr else None,
               "peak": ltp,           # best price seen IN THE TRADE'S FAVOUR
               "pnl": 0.0, "pnl_ts": time.time(), "margin": need,
               "opened": now_ist().strftime("%H:%M:%S"),
               "opened_date": now_ist().strftime("%Y-%m-%d"),
               "expiry": self.bus.get(f"future_expiry:{symbol}"),
               "order_id": order_id, "paper": not live,
               "mae": 0.0, "mfe": 0.0}
        # 2026-07-28 — defense zone (see _monitor_futures), mirroring
        # the mechanism spreads already have: act BEFORE a full stop
        # breach, not only at it. Captured here, separately from
        # pos["sl"], because that field gets moved FAVOURABLY by the
        # trailing mechanism once in profit — the defense zone needs
        # the ORIGINAL risk distance (entry to the stop as first set),
        # not whatever the current (possibly already-trailed) stop is.
        pos["initial_sl"] = pos["sl"]
        pos["defended"] = False
        # v59.72 (R2 finding H4 family) — re-read at write time. Writing
        # the earlier loop/method-local dict back wholesale can resurrect
        # a position another path just closed.
        _cur = self.bus.get("futures_positions", {}) or {}
        _cur[symbol] = pos
        self.bus.set("futures_positions", _cur)
        self.bus.log(self.name, f"FUT {side} {symbol} \u00d7{lots} lot(s) "
                     f"@ {ltp} (SL {pos['sl']}, T {pos['target']}, "
                     f"margin \u20b9{need:,.0f}) [{'live' if live else 'paper'}]")
        self.bus.alert("medium", self.name, symbol,
                       f"Futures {side} opened @ {ltp} \u00d7{lots} lot(s)")
        return {"ok": True, "position": pos}

    def _monitor_futures(self):
        futs = self.bus.get("futures_positions", {}) or {}
        if not futs:
            return
        cfg = config.load()
        for sym in list(futs.keys()):
            p = futs[sym]
            ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
            ltp = ohlc.get("close")
            if not ltp:
                if not market_open():
                    # stale post-close feed must not block EOD handling —
                    # same fix as the options stuck-open-past-close bug
                    self.exit_future(sym, "EOD square-off (no live feed)")
                continue
            # v59.69 (third-eye Tier 3) — same two guards the options
            # monitor got: after close, square off and evaluate nothing
            # (the 15:15 EOD branch below only ran if this loop reached
            # it with a live-looking price); during hours, hold decisions
            # on a stale quote instead of trailing stops off it. The ts
            # key is new — futures quotes had no timestamp to check.
            if not market_open():
                p["ltp"] = ltp
                sign = 1 if p["side"] == "LONG" else -1
                p["pnl"] = round((ltp - p["entry"]) * sign
                                 * p["lot_size"] * p["lots"], 2)
                # v59.72 (R2 finding H4) — write ONLY this symbol against
                # a fresh read. Writing the loop-local dict re-inserted a
                # position exit_future had already popped, so with two
                # contracts open at close the first was squared off TWICE
                # (double-booked trade; in live, a second offsetting order).
                _cur = self.bus.get("futures_positions", {}) or {}
                if sym in _cur:
                    _cur[sym] = p
                    self.bus.set("futures_positions", _cur)
                self.exit_future(sym, "market closed — squaring off")
                continue
            _fts = ohlc.get("ts")
            _max_age = cfg.get("exit_quote_max_age_sec", 90)
            if _fts and time.time() - _fts > _max_age:
                if time.time() - getattr(self, "_fut_stale_log_ts", 0) > 60:
                    self._fut_stale_log_ts = time.time()
                    self.bus.log(self.name,
                                 f"{sym} futures quote "
                                 f"{time.time() - _fts:.0f}s old "
                                 f"(>{_max_age}s) — holding exit decisions")
                continue
            sign = 1 if p["side"] == "LONG" else -1
            gross = (ltp - p["entry"]) * sign * p["lot_size"] * p["lots"]
            p["pnl"] = round(gross, 2)
            p["pnl_ts"] = time.time()   # v59.70 — freshness stamp
            p["ltp"] = ltp
            p["mfe"] = max(p.get("mfe", 0), gross)
            p["mae"] = min(p.get("mae", 0), gross)
            # trailing: once favourable move exceeds the trigger, trail
            # the stop behind the best favourable price (direction-aware)
            if cfg.get("trail_sl_enabled", True):
                trig = cfg.get("futures_trail_trigger_pct", 0.3) / 100
                gap = cfg.get("futures_trail_gap_pct", 0.2) / 100
                fav = (ltp - p["entry"]) * sign
                best = (p["peak"] - p["entry"]) * sign
                if fav > best:
                    p["peak"] = ltp
                if (p["peak"] - p["entry"]) * sign >= p["entry"] * trig:
                    trail_to = round(p["peak"] * (1 - sign * gap), 2)
                    if (trail_to - p["sl"]) * sign > 0:
                        p["sl"] = trail_to
            # 2026-07-28 — defense zone, per explicit request, mirroring
            # the mechanism spreads already have (_monitor_spreads'
            # own "act BEFORE a full breach, not only at it"). Adapted
            # for futures: there's no strike/gamma concept here, P&L is
            # linear, so the equivalent danger point is simply the
            # ORIGINAL stop-loss price itself. Once an ADVERSE move
            # (price moving toward loss, not the favourable trailing
            # case above) has consumed a configured fraction of the
            # original entry-to-stop distance, tighten the stop closer
            # to the current price instead of waiting for the full
            # original stop to be reached. One-shot per position (the
            # `defended` flag, same pattern as spreads) — this is a
            # single, deliberate tightening once real danger is close,
            # not a continuous ratchet.
            if (cfg.get("futures_defense_enabled", True) and not p.get("defended")
                    and "initial_sl" in p):
                risk_distance = abs(p["entry"] - p["initial_sl"])
                # adverse_move > 0 means price has moved AGAINST the
                # position (toward the stop), regardless of side —
                # for LONG (sign=1): adverse = entry - ltp (price fell)
                # for SHORT (sign=-1): adverse = (entry - ltp) * -1 =
                # ltp - entry (price rose). Mirrors `fav` in the
                # trailing block above exactly, just negated (adverse
                # is the opposite of favourable).
                adverse_move = (p["entry"] - ltp) * sign
                if risk_distance > 0 and adverse_move > 0:
                    zone = risk_distance * cfg.get("futures_defense_zone_pct", 40) / 100
                    if adverse_move >= risk_distance - zone:
                        remaining_room = max(0, risk_distance - adverse_move)
                        tightened_room = remaining_room * cfg.get(
                            "futures_defense_tighten_pct", 50) / 100
                        new_sl = round(ltp - sign * tightened_room, 2)
                        # Only ever tighten (move closer to current
                        # price), never loosen — same safety direction
                        # as the trailing mechanism's own guard.
                        if (new_sl - p["sl"]) * sign > 0:
                            old_sl = p["sl"]
                            p["sl"] = new_sl
                            p["defended"] = True
                            self.bus.log(self.name,
                                        f"\U0001f6e1 {sym} futures defense triggered — "
                                        f"adverse move {adverse_move:.1f}pts has consumed "
                                        f"{adverse_move/risk_distance*100:.0f}% of the "
                                        f"original {risk_distance:.1f}pt risk distance, "
                                        f"stop tightened {old_sl:.1f} \u2192 {new_sl:.1f}")
                            self.bus.alert("medium", self.name, sym,
                                          f"Futures {p['side']} defense: "
                                          f"stop tightened to {new_sl:.1f}")
            # v59.72 (R2 finding H4, pre-existing variant) — same rule:
            # per-symbol write against a fresh read, never the whole
            # loop-local dict.
            _cur = self.bus.get("futures_positions", {}) or {}
            if sym in _cur:
                _cur[sym] = p
                self.bus.set("futures_positions", _cur)
            hm = now_ist().hour * 60 + now_ist().minute
            _rpf_fut = rupee_profit_floor(p, p.get("pnl", 0), cfg, "futures")
            # 2026-08-01 — translate the ARMED floor into a stop PRICE.
            #
            # The floor was a P&L comparison evaluated once per monitor
            # cycle: it fires when pnl <= floor, but nothing holds pnl
            # NEAR the floor. When price ran between cycles it fired at
            # whatever P&L existed by then, and the reason string quoted
            # the floor rather than the fill. From the journal:
            #
            #   "gave back to ₹2310 of peak ₹4200"  ->  booked gross -₹3,000
            #   "gave back to ₹495  of peak  ₹900"  ->  booked gross -₹2,340
            #   "gave back to ₹1551 of peak ₹2820"  ->  booked gross -₹1,980
            #
            # Four such exits cost ₹10,620 while reporting that they had
            # protected a profit. A floor expressed in P&L can only be
            # observed at cycle granularity; expressed as a PRICE it
            # becomes the stop, which is checked first in this chain and
            # exits AT the level rather than wherever the cycle lands.
            #
            # Ratchets one way only — it may tighten the stop toward the
            # floor, never loosen it — so it composes with the ATR trail
            # and the defense-zone tightening above.
            if cfg.get("rupee_profit_floor_as_stop", True):
                _fl = float(p.get("rpf_floor", 0) or 0)
                _q = (p.get("lot_size") or 0) * (p.get("lots") or 0)
                if _fl > 0 and _q:
                    _fpx = round(p["entry"] + sign * (_fl / _q), 2)
                    if (_fpx - p["sl"]) * sign > 0:
                        _old = p["sl"]
                        p["sl"] = _fpx
                        self.bus.log(self.name,
                                     f"{sym} profit floor → stop {_old} → {_fpx} "
                                     f"(locks ₹{_fl:,.0f} of peak "
                                     f"₹{float(p.get('rpf_peak', 0)):,.0f})")
            if (ltp - p["sl"]) * sign <= 0:
                self.exit_future(sym, f"stoploss ({p['sl']})")
            elif (ltp - p["target"]) * sign >= 0:
                self.exit_future(sym, f"target ({p['target']})")
            elif _rpf_fut:
                # Futures had NO profit protection whatsoever before
                # this. Today: MFE ₹11,934 across futures -> realised
                # -₹645, every position closed by EOD square-off or the
                # portfolio kill-switch, not one protective exit.
                self.exit_future(sym, _rpf_fut)
            elif hm >= 15 * 60 + 15:
                self.exit_future(sym, "EOD square-off (15:15)")
            else:
                self._futures_ai_check(p, sym, ltp)

    def _futures_ai_check(self, p, sym, ltp):
        """2026-07-28 — per explicit request, mirrors _spread_ai_check/
        _option_ai_check exactly (same advisory-only-by-default design,
        same 5-minute cadence, same auto-exit opt-in pattern) but for
        futures positions, which had no equivalent advisory at all
        until now. Includes the market-move context (see
        _market_move_context) so the advisory factors in where price
        may go next, not just the position's own static entry/SL/
        target numbers."""
        cfg = config.load()
        if cfg.get("ai_engine", "local") == "off":
            return
        _risk = abs(p.get("entry", 0) - p.get("initial_sl", p.get("sl", 0))) \
            * (p.get("qty") or 0)
        _near = False
        if p.get("sl") and p.get("entry"):
            _span = abs(p["entry"] - p["sl"]) or 1
            _near = abs(p.get("ltp", p["entry"]) - p["sl"]) / _span < 0.25
        _trig = ai_advisory_due(p, cfg, p.get("pnl", 0), _risk, _near)
        if not _trig:
            return
        p["ai_ts"] = time.time()
        p["ai_last_pnl"] = p.get("pnl", 0)
        # v59.71 (third-eye Tier 4) — the exit DECISION is made inside
        # the advice try; the exit CALL runs after it. It used to sit
        # inside: any bug raised by exit_future() — a KeyError on a
        # restored position, a cost-model TypeError — was converted into
        # the cosmetic string "AI check unavailable", and a position the
        # system had decided to close silently stayed open. An advice
        # failure may degrade quietly; an exit failure must surface.
        _do_exit = None
        try:
            import llm, json as _json
            _t0 = time.time()
            market_ctx = self._market_move_context(sym)
            prompt = (
                "You monitor an open Indian index futures position. "
                "Reply ONLY JSON: {\"advice\":\"HOLD|EXIT\",\"confidence\":0-100,"
                "\"why\":\"<15 words\"}.\n"
                f"{sym} {p['side']} \u00d7{p['lots']} lot(s), entry "
                f"{p['entry']}, current {ltp}, stop {p['sl']}, target "
                f"{p['target']}, current P&L \u20b9{p.get('pnl', 0)}. "
                f"Market context (factor in where price may move next, "
                f"not just the position's own numbers): {market_ctx}.")
            text, engine, err = llm.generate_json(prompt, max_tokens=120)
            if err or not text:
                p["ai_advice"] = None if err == "ai_off" else f"AI unavailable ({err})"
                return
            j = _json.loads(text)
            if j and j.get("advice"):
                p["ai_advice"] = (f"{j['advice']} ({j.get('confidence', '?')}%)"
                                 f" — {j.get('why', '')} \u00b7 {engine}")
                confidence = int(j.get("confidence", 0))
                threshold = cfg.get("futures_ai_exit_confidence_threshold", 75)
                _log_ai_advisory(self, sym, "futures", j, confidence,
                                 threshold, cfg, p.get("pnl", 0),
                                 _trig, time.time() - _t0)
                if j["advice"] == "EXIT" and confidence >= threshold:
                    why = j.get("why", "")
                    self.bus.alert("medium", "execution", sym,
                                   f"AI suggests exiting {sym} futures "
                                   f"{p['side']}: {why}")
                    if cfg.get("futures_ai_auto_exit_enabled", False):
                        self.bus.log(self.name,
                                     f"AI auto-exit ENABLED — closing {sym} "
                                     f"futures {p['side']} on AI advisory "
                                     f"({confidence}%): {why}")
                        _do_exit = f"AI advisory EXIT ({confidence}%): {why}"
        except Exception as e:
            p["ai_advice"] = f"AI check unavailable ({e})"
        if _do_exit:
            self.exit_future(sym, _do_exit)   # outside the try — see above

    def _futures_signal_engine(self, symbol=None):
        """S4 Phase 2 (v52) — futures entry-signal engine. Explicit
        design decision: HYBRID.

          BASE direction — the SAME regime + multi-timeframe-confluence
          gate every directional options strategy already runs through
          (RegimeAgent's regime:{sym}/allowed_signals/confluence). This
          is not a new indicator; it is the existing "is today a
          trending session, and do 1m/5m/15m agree" read, mapped from
          BUY_CE/BUY_PE onto LONG/SHORT.

          CONFIRMATION — a futures-SPECIFIC gate: the current-month
          contract's own OI-buildup direction (future_oi_trend:{sym},
          already computed by MarketDataAgent._classify_future_tick,
          previously only a SOFT confidence nudge inside
          mtf_confluence_strategy). Here it is a required confirmation,
          because the instrument being traded IS the future — its own
          positioning agreeing with the spot-side regime read matters
          more than it does for an option buy. Kept to the same
          directional-conflict convention as every other gate in this
          codebase (news risk/opportunity, §7.2): missing data SKIPS
          the gate, only an ACTUAL conflict blocks.

        Runs on ExecutionAgent's 2s cycle but is internally cooled down
        per symbol (futures_cooldown_min) — a directional regime read
        doesn't change fast enough to re-evaluate every tick, and this
        avoids hammering enter_future() with the exact same rejected
        attempt every cycle.
        """
        cfg = config.load()
        if not cfg.get("futures_strategy_enabled", False):
            return
        if not cfg.get("futures_auto_deploy", False):
            return
        if not market_open():
            return
        if time.time() < self.bus.get("portfolio_halt_until", 0):
            return
        if not hasattr(self, "_fut_sig_state"):
            self._fut_sig_state = {}   # sym -> {cool_until, taken, day}
        symbols = [symbol] if symbol else self.bus.get("symbols", [])
        today = now_ist().strftime("%Y-%m-%d")
        for sym in symbols:
            st = self._fut_sig_state.setdefault(
                sym, {"cool_until": 0, "taken": 0, "day": today})
            if st["day"] != today:
                st.update(cool_until=0, taken=0, day=today)
            if sym in (self.bus.get("futures_positions", {}) or {}):
                continue    # one position per symbol, same as options
            if time.time() < st["cool_until"]:
                continue
            if st["taken"] >= cfg.get("futures_max_trades_per_day", 2):
                continue
            ev, _gates = self._futures_signal_eval(sym, cfg)
            if not ev:
                continue
            r = self.enter_future(sym, ev["side"], ev["lots"])
            st["cool_until"] = time.time() + cfg.get("futures_cooldown_min", 30) * 60
            if r.get("ok"):
                st["taken"] += 1
                self.bus.log(self.name, f"{sym}: futures auto-entry — "
                             f"{ev['why']}")
            else:
                # enter_future's own gates (margin, kill-switch, etc.)
                # still apply and can reject — log why rather than
                # retrying identically next cycle (cooldown covers that).
                self.bus.log(self.name, f"{sym}: futures signal fired but "
                             f"entry was rejected — {r.get('error')}")

    def _futures_signal_eval(self, sym, cfg):
        """Pure evaluation, no side effects — returns (signal_or_None,
        gates). Split out from _futures_signal_engine so the Strategies
        page can show LIVE eligibility the same way S7's card does,
        without needing auto-deploy on (visibility without deployment).
        `gates` is always populated, even on a None result, so the API
        can show WHY nothing fired rather than just "no"."""
        gates = {"regime": "skipped (no regime data yet)",
                 "confluence": "skipped (no regime data yet)",
                 "oi_confirm": "skipped (no OI buildup data yet)"}
        regime = self.bus.get(f"regime:{sym}") or {}
        if not regime:
            return None, gates
        if regime.get("stale"):
            gates["regime"] = "skipped (regime data is stale)"
            return None, gates
        allowed = regime.get("allowed_signals", [])
        confidence = regime.get("confidence", 0)
        confluence = regime.get("confluence", "no-alignment")
        min_conf = cfg.get("futures_min_regime_confidence", 60)
        long_ok = "BUY_CE" in allowed and confluence in ("strong-bull", "mixed-bull")
        short_ok = "BUY_PE" in allowed and confluence in ("strong-bear", "mixed-bear")
        gates["regime"] = regime.get("regime", "unknown")
        gates["confluence"] = confluence
        if not (long_ok or short_ok) or confidence < min_conf:
            return None, gates
        side = "LONG" if long_ok else "SHORT"
        if cfg.get("futures_require_oi_confirm", True):
            oi_trend = self.bus.get(f"future_oi_trend:{sym}")
            if oi_trend is None:
                gates["oi_confirm"] = "skipped (no futures OI data yet)"
            else:
                conflict = ((side == "LONG" and oi_trend == "short") or
                           (side == "SHORT" and oi_trend == "long"))
                if conflict:
                    gates["oi_confirm"] = (f"BLOCKED (futures showing "
                                          f"{oi_trend} buildup, opposing {side})")
                    return None, gates
                gates["oi_confirm"] = f"confirmed ({oi_trend} buildup agrees)"
        ohlc = self.bus.get(f"future_ohlc:{sym}") or {}
        ltp = ohlc.get("close")
        if not ltp:
            gates["sizing"] = "skipped (no live futures price yet)"
            return None, gates
        # v58.39 — SENSEX dropped from futures by default: 10% win rate
        # over 10 trades, and its data pipeline is separately broken (0
        # archived chain days, websocket never ticks its options). No
        # strategy fix helps an instrument whose data is wrong.
        allowed = cfg.get("futures_symbols") or ["NIFTY", "BANKNIFTY", "FINNIFTY"]
        if sym not in allowed:
            gates["symbol"] = f"blocked ({sym} not in futures_symbols)"
            return None, gates
        _blk = class_budget_blocked(cfg, self.bus.get("closed_today") or [],
                                    "futures")
        if _blk:
            gates["budget"] = f"blocked ({_blk})"
            log_futures_shadow(self.bus, sym, side, gates, False, _blk)
            return None, gates
        gates["budget"] = "within futures daily budget"
        sign = 1 if side == "LONG" else -1
        import sizing
        # ATR-based stop, falling back to the old fixed percentage when
        # candles are unavailable. Index 5m candles are the ATR proxy —
        # a futures contract tracks its index closely enough in POINTS
        # for this purpose, and it is data we already hold.
        _atr = None
        if cfg.get("futures_stop_mode", "atr") == "atr":
            _pack = self.bus.get(f"pa_candles:{sym}") or {}
            _atr = sizing.atr_points(_pack.get("c5") or [],
                                     cfg.get("futures_atr_period", 14))
        if _atr:
            stop_pts = _atr * cfg.get("futures_atr_stop_mult", 1.5)
            stop = ltp - sign * stop_pts
            gates["stop"] = (f"ATR({cfg.get('futures_atr_period', 14)}) "
                             f"{_atr:.0f}pts x {cfg.get('futures_atr_stop_mult', 1.5)} "
                             f"= {stop_pts:.0f}pt stop")
        else:
            sl_pct = cfg.get("futures_sl_pct", 0.4) / 100
            stop = ltp * (1 - sign * sl_pct)
            gates["stop"] = f"fixed {cfg.get('futures_sl_pct', 0.4)}% (no ATR yet)"
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        futs = self.bus.get("futures_positions", {}) or {}
        deployed = sizing.deployed_capital(cfg, positions, spreads, futs)
        n_lots, sizing_why = sizing.size_future(cfg, sym, ltp, stop, deployed)
        gates["sizing"] = sizing_why
        if n_lots < 1:
            # The rupee cap and margin check both land here. Without a
            # record, an over-tight cap is indistinguishable from a
            # correctly-filtered bad trade.
            log_futures_shadow(self.bus, sym, side, gates, False, sizing_why,
                               ltp=ltp, stop=stop)
            return None, gates
        log_futures_shadow(self.bus, sym, side, gates, True, "eligible",
                           ltp=ltp, lots=n_lots, stop=stop)
        return {"side": side, "lots": n_lots,
                "why": f"regime '{gates['regime']}' + confluence "
                      f"'{confluence}' (confidence {confidence}%) + "
                      f"{gates['oi_confirm']} — {sizing_why}"}, gates

    def exit_future(self, symbol, reason="manual exit"):
        futs = self.bus.get("futures_positions", {}) or {}
        p = futs.pop(symbol, None)
        if not p:
            return {"error": f"no open futures position on {symbol}"}
        self.bus.set("futures_positions", futs)
        cfg = config.load()
        # 2026-07-26 (v52) — a LIVE position needs a real OFFSETTING
        # order (SELL to close a LONG, BUY to close a SHORT), not just a
        # bookkeeping close. Best-effort: if the close order fails, the
        # position is still removed from tracking (mirroring it here
        # forever on a broker error would be worse — the actual
        # position lives at the broker regardless of what this app
        # thinks), but the failure is logged loudly and the exit reason
        # is annotated so it isn't silently indistinguishable from a
        # clean close.
        if not p.get("paper", True):
            orders = self.ctx.get("orders_factory", lambda: None)()
            front = (self.bus.get(f"future_months:{symbol}") or {}).get("front")
            if orders and front and front.get("security_id"):
                try:
                    resp = orders.place(
                        symbol, front["security_id"],
                        "SELL" if p["side"] == "LONG" else "BUY",
                        p["lots"] * p["lot_size"], "MARKET")
                    self.bus.log(self.name, f"\U0001f534 LIVE FUT close order "
                                 f"{symbol} — "
                                 f"{resp.get('orderId') or 'UNCONFIRMED'}")
                    _st = self._confirm_order(orders, resp,
                                              f"FUT close {symbol}")
                    # v59.72 (R2 finding H2) — a REJECTED close must not
                    # book the exit; the contract is still live.
                    if _st in ("REJECTED", "CANCELLED"):
                        return {"error": f"FUT close {_st} at the broker — "
                                         f"position kept, verify manually"}
                    # v59.75 — exit P&L from the REAL fill when the
                    # trade book answers.
                    _fpx, _ = self._actual_fill(
                        orders, resp, p["lots"] * p["lot_size"],
                        f"FUT close {symbol}")
                    if _fpx:
                        _sign = 1 if p["side"] == "LONG" else -1
                        p["exit_fill_slippage"] = round(
                            _fpx - (p.get("ltp") or _fpx), 2)
                        p["ltp"] = _fpx
                        p["pnl"] = round((_fpx - p["entry"]) * _sign
                                         * p["lot_size"] * p["lots"], 2)
                except Exception as e:
                    reason = f"{reason} [LIVE CLOSE ORDER FAILED: {e} — " \
                             f"verify position at the broker manually]"
                    # v59.70 — an ALERT, not only a feed line: same
                    # urgency class as the option exit failure.
                    self.bus.alert("high", self.name, symbol,
                                   f"LIVE FUT close FAILED for {symbol} "
                                   f"({type(e).__name__}: {e}) — close "
                                   f"manually at the broker NOW")
            else:
                reason = f"{reason} [no broker/security_id for live close " \
                         f"— verify position at the broker manually]"
        _c = realistic_costs("future", symbol, p["lots"],
                             p.get("entry"), p.get("ltp") or p.get("entry"),
                             cfg, log=lambda m: self.bus.log(self.name, m))
        fees, slippage = _c["fees"], _c["slippage"]
        warn_zero_fees(self.bus, self.name, "position", p.get("lots"), fees)
        gross = p.get("pnl", 0)
        now = now_ist()
        # v58.49 (roadmap B1) — exit marker, classified the same way
        # options are so the chart legend reads consistently across
        # instrument types.
        _k = ("target_hit" if "target" in str(reason).lower()
              else "stop_hit" if "stop" in str(reason).lower() else "exit")
        _idx = (self.bus.get(f"analysis:{symbol}") or {}).get("spot")
        self._record_chart_event(symbol, _k, _idx or None,
                                 f"FUT {p.get('side')} exit "
                                 f"₹{p.get('pnl', 0):.0f}")
        closed = dict(p, closed=now.strftime("%H:%M:%S"),
                      closed_date=now.strftime("%Y-%m-%d"),
                      closed_at=now.isoformat(),
                      gross_pnl=gross, fees=fees, slippage=slippage,
                      cost_model=_c.get("model"),   # v59.68 — fallback is visible in the record
                      pnl=round(gross - fees - slippage, 0),   # NET of BOTH cost parts
                      reason=reason)
        _record_closed(self.bus, closed)   # capped window (v59.71)
        _append_trade(closed)
        self.bus.log(self.name, f"FUT exit {p['side']} {symbol} — {reason} — "
                     f"gross \u20b9{gross:.0f}, fees \u20b9{fees:.0f}, "
                     f"slippage \u20b9{slippage:.0f}, "
                     f"net \u20b9{gross - fees - slippage:.0f}")
        self.bus.alert("high", self.name, symbol,
                       f"Futures {p['side']} closed — {reason} — "
                       f"net \u20b9{gross - fees - slippage:.0f}")
        self.bus.publish("closed", closed)
        return {"ok": True, "closed": closed}

    def _check_portfolio_kill_switch(self):
        """Regression testing (2026-07-20) surfaced a real gap: the
        daily loss limit only gates NEW entries against REALIZED P&L —
        it does nothing if several OPEN positions move against you
        together mid-event (a correlated crash across NIFTY/BANKNIFTY/
        FINNIFTY/SENSEX, exactly the scenario tested). This checks
        combined UNREALIZED P&L across every open position and spread,
        every cycle (2s), and force-closes everything if it breaches
        a configured threshold — independent of and in addition to the
        per-trade risk checks."""
        cfg = config.load()
        if not cfg.get("portfolio_kill_switch_enabled", True):
            return
        halted_until = self.bus.get("portfolio_halt_until", 0)
        if time.time() < halted_until:
            return   # already tripped this cooldown window
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        futures = self.bus.get("futures_positions", {}) or {}   # S4 (v50)
        total_unrealized = (sum(p.get("pnl", 0) for p in positions.values())
                           + sum(s.get("pnl", 0) for s in spreads.values())
                           + sum(f.get("pnl", 0) for f in futures.values()))
        limit = cfg.get("portfolio_max_drawdown", 15000)
        # v59.70 (third-eye Tier 3, round 2) — the sum above is only as
        # fresh as its inputs, and each pnl freezes at its last value
        # (or the 0.0 seeded at entry) when the feed dies. The switch
        # cannot distinguish "flat" from "unknown"; it CAN say so out
        # loud instead of silently guarding on numbers nobody is
        # updating. Throttled; market hours only (a weekend book is
        # legitimately unmonitored).
        _max_age = 2 * int(cfg.get("exit_quote_max_age_sec", 90) or 90)
        _now = time.time()
        _stale_n = sum(1 for book in (positions, spreads, futures)
                       for x in book.values()
                       if _now - (x.get("pnl_ts") or 0) > _max_age)
        if _stale_n and market_open() and \
                _now - getattr(self, "_ks_stale_alert_ts", 0) > 300:
            self._ks_stale_alert_ts = _now
            self.bus.alert("medium", self.name, "PORTFOLIO",
                           f"kill-switch input STALE for {_stale_n} open "
                           f"exposure(s) (no fresh pnl in {_max_age}s) — "
                           f"combined unrealized ₹{total_unrealized:.0f} is "
                           f"UNVERIFIED until the feed recovers")
        if not (positions or spreads or futures) or total_unrealized > -abs(limit):
            return
        # breach — force-close everything, no waiting for individual
        # stops/targets to catch up
        self.bus.log(self.name,
                     f"🚨 PORTFOLIO KILL-SWITCH: combined unrealized ₹{total_unrealized:.0f} "
                     f"breached -₹{limit} across {len(positions)} position(s) + "
                     f"{len(spreads)} spread(s) — force-closing everything")
        self.bus.alert("high", self.name, "PORTFOLIO",
                       f"KILL-SWITCH TRIPPED: ₹{total_unrealized:.0f} combined "
                       f"unrealized loss — all positions closed, new entries "
                       f"blocked for {cfg.get('portfolio_halt_cooldown_min', 60)}m")
        for sym in list(positions.keys()):
            self.exit(f"portfolio kill-switch (combined ₹{total_unrealized:.0f})",
                     symbol=sym)
        for sid in list(spreads.keys()):
            self.exit_spread(sid, f"portfolio kill-switch (combined ₹{total_unrealized:.0f})")
        for sym in list(futures.keys()):
            self.exit_future(sym, f"portfolio kill-switch (combined ₹{total_unrealized:.0f})")
        cooldown = cfg.get("portfolio_halt_cooldown_min", 60) * 60
        self.bus.set("portfolio_halt_until", time.time() + cooldown)

    def _confirm_order(self, orders, resp, context):
        """Best-effort post-placement status check (v59.70, third-eye
        Tier 3 round 2). `order_status()` had ZERO call sites: no order
        was ever confirmed after placement, so a broker-side rejection —
        margin shortfall, freeze-quantity breach, circuit limit — left
        the book believing in a fill that never happened. One poll,
        immediately after placing; never raises; REJECTED/CANCELLED is a
        HIGH alert. Returns the status string or None (= unverified,
        which is logged as such, never read as OK)."""
        oid = (resp or {}).get("orderId")
        if not oid or not hasattr(orders, "order_status"):
            return None
        try:
            st = orders.order_status(str(oid)) or {}
            # v59.72 (R2 finding H3) — Dhan's GET /orders/{id} returns a
            # JSON ARRAY of order objects. The old .get() on a list
            # raised AttributeError into this function's own catch, so
            # the entire fill-confirmation feature was a silent no-op
            # logging "unavailable" — the same shape _reconcile_broker
            # always handled and this function did not.
            if isinstance(st, list):
                st = st[0] if st else {}
            data = st.get("data") if isinstance(st.get("data"), (dict, list)) \
                else st
            if isinstance(data, list):
                data = data[0] if data else {}
            status = str((data or {}).get("orderStatus")
                         or (data or {}).get("status") or "").upper()
            if status in ("REJECTED", "CANCELLED"):
                self.bus.alert("high", self.name, context,
                               f"order {oid} {status} at the broker — the "
                               f"book does NOT reflect a fill; check margin/"
                               f"freeze-quantity/circuit limits ({context})")
            elif status:
                self.bus.log(self.name,
                             f"order {oid} status: {status} ({context})")
            return status or None
        except Exception as e:
            self.bus.log(self.name,
                         f"order {oid} status check unavailable "
                         f"({type(e).__name__}) — UNVERIFIED ({context})")
            return None

    def _order_ws_manage(self):
        """v59.76 — lifecycle for the Dhan order-update websocket.

        Runs every cycle, cheap: connect only when live (paper mode has
        no broker orders to hear about), `order_update_ws_enabled`, the
        broker is Dhan and credentials exist; disconnect the moment any
        of that stops being true. The socket is a BELT on top of the
        polling confirm and the reconciler — losing it degrades to the
        v59.75 behaviour, never below it."""
        cfg = config.load()
        want = (not cfg.get("paper_mode", True)
                and cfg.get("order_update_ws_enabled", True)
                and cfg.get("broker", "dhan") == "dhan"
                and cfg.get("dhan_client_id")
                and cfg.get("dhan_access_token"))
        client = getattr(self, "_order_ws", None)
        if want and client is None:
            import dhan_order_ws
            client = dhan_order_ws.OrderUpdateClient(
                cfg.get("dhan_client_id"), cfg.get("dhan_access_token"),
                on_event=self._on_order_event,
                log=lambda m: self.bus.log(self.name, m))
            client.start()
            self._order_ws = client
            self.bus.log(self.name, "order-update websocket starting "
                                    "(live mode)")
        elif not want and client is not None:
            client.stop()
            self._order_ws = None
            self.bus.log(self.name, "order-update websocket stopped "
                                    "(paper mode / disabled)")
        self.bus.set("order_ws",
                     client.status() if client else {"state": "off"})

    def _on_order_event(self, msg):
        """Order-alert consumer (runs on the websocket thread).

        v59.76 — three jobs, all report-and-repair, none order-placing:
          * resolve UNCONFIRMED order ids the moment Dhan answers
            (matched by order_id, else by security_id on a position
            whose id is still UNCONFIRMED*);
          * REJECTED/CANCELLED on a tracked order → HIGH alert — the
            book holds a phantom until reconciled;
          * a traded price on a still-open entry with no recorded fill
            → book the real fill (same fields _actual_fill writes).
        Writes are per-symbol against a fresh bus read (the H4 rule)."""
        import dhan_order_ws
        ev = dhan_order_ws.normalize_event(msg)
        if ev is None:
            return
        feed = self.bus.get("order_update_feed", [])
        feed.append({**{k: ev[k] for k in
                        ("order_id", "status", "security_id",
                         "traded_qty", "avg_price")},
                     "ts": time.time()})
        self.bus.set("order_update_feed", feed[-100:])
        self.bus.log(self.name,
                     f"order update: {ev.get('order_id')} "
                     f"{ev.get('status')} qty {ev.get('traded_qty')} "
                     f"@ {ev.get('avg_price')}")
        for book in ("positions", "futures_positions"):
            cur = self.bus.get(book, {}) or {}
            for sym, p in list(cur.items()):
                oid = str(p.get("order_id") or "")
                matched = (ev["order_id"] and oid == ev["order_id"]) or \
                          (oid.startswith("UNCONFIRMED")
                           and ev["security_id"]
                           and str(p.get("security_id") or "")
                           == ev["security_id"])
                if not matched:
                    continue
                if oid.startswith("UNCONFIRMED") and ev["order_id"]:
                    p["order_id"] = ev["order_id"]
                    self.bus.log(self.name,
                                 f"order id resolved via feed: {sym} → "
                                 f"{ev['order_id']}")
                if ev["status"]:
                    p["order_status"] = ev["status"]
                if ev["status"] in dhan_order_ws.TERMINAL_BAD:
                    self.bus.alert("high", self.name, sym,
                                   f"order {ev['order_id']} {ev['status']} "
                                   f"at the broker — the tracked {sym} "
                                   f"position may be a PHANTOM; reconcile "
                                   f"before trusting any exit")
                elif ev["avg_price"] and not p.get("entry_fill_slippage") \
                        and p.get("entry"):
                    p["quote_at_entry"] = p.get("quote_at_entry",
                                                p["entry"])
                    p["entry_fill_slippage"] = round(
                        ev["avg_price"] - p["quote_at_entry"], 2)
                    p["entry"] = ev["avg_price"]
                fresh = self.bus.get(book, {}) or {}
                if sym in fresh:
                    fresh[sym] = p
                    self.bus.set(book, fresh)

    def _actual_fill(self, orders, resp, expect_qty=None, context=""):
        """Best-effort REAL fill from the trade book (v59.75).

        Third-eye Tier 0/3: 'actual fill prices are never learned —
        entry is recorded as the pre-trade quote, exit as the last
        monitored premium.' For LIVE orders this asks the broker what
        actually traded (broker_adapter.parse_fills, the one fill
        parser) and returns (avg_price or None, qty). None means
        unverified — callers keep the quote and the record says so via
        the absent fill fields; nothing is invented. A partial fill is
        a HIGH alert: the book tracks intended qty and must be
        reconciled by hand until order-splitting exists."""
        oid = (resp or {}).get("orderId")
        if not oid or not hasattr(orders, "trade_book"):
            return None, 0
        try:
            import broker_adapter as _ba
            px, qty = _ba.parse_fills(orders.trade_book(oid), order_id=oid)
            if px:
                self.bus.log(self.name,
                             f"fill: order {oid} avg ₹{px} ×{qty} ({context})")
                if expect_qty and qty and int(qty) != int(expect_qty):
                    self.bus.alert("high", self.name, context,
                                   f"PARTIAL FILL {qty}/{expect_qty} "
                                   f"({context}) — the book tracks the "
                                   f"intended qty; reconcile manually")
            return px, qty
        except Exception as e:
            self.bus.log(self.name,
                         f"fill lookup unavailable for {oid} "
                         f"({type(e).__name__}) — keeping the quote "
                         f"({context})")
            return None, 0

    def _reconcile_broker(self):
        """v59.69 (third-eye Tier 3) — close the book against broker
        ground truth. `DhanOrders.positions()` had existed with ZERO
        call sites: no order was ever confirmed, no position ever
        checked, and a restart trusted open_state.json unconditionally —
        a position squared off by the broker (or closed by hand in the
        app) would be restored as open and SOLD A SECOND TIME on its
        next exit. Live mode only: paper has no broker book to check.

        Report-only: a mismatch raises a HIGH alert and is published on
        the bus; nothing here mutates the book. Auto-correcting from a
        possibly-partial broker read is how a transient API error would
        wipe real positions.
        """
        cfg = config.load()
        if cfg.get("paper_mode", True):
            return
        interval = int(cfg.get("broker_reconcile_interval_sec", 300) or 300)
        if time.time() - getattr(self, "_last_reconcile_ts", 0) < interval:
            return
        self._last_reconcile_ts = time.time()
        try:
            orders = self.ctx["orders_factory"]()
            if orders is None or not hasattr(orders, "positions"):
                return
            raw = orders.positions() or []
        except Exception as e:
            # Unverifiable is said out loud, not silently skipped —
            # "could not check" must never read as "checked, clean".
            self.bus.log(self.name, f"⚠ broker reconcile unavailable "
                                    f"({type(e).__name__}: {e}) — book "
                                    f"UNVERIFIED this interval")
            return
        rows = raw.get("data") if isinstance(raw, dict) else raw
        broker = {}
        for r in rows or []:
            sid = str(r.get("securityId") or r.get("security_id") or "")
            try:
                qty = int(r.get("netQty", r.get("net_qty", 0)) or 0)
            except (TypeError, ValueError):
                continue
            if sid:
                broker[sid] = broker.get(sid, 0) + qty
        mine = {}
        for p in (self.bus.get("positions", {}) or {}).values():
            sid = str(p.get("security_id") or "")
            if sid:
                mine[sid] = mine.get(sid, 0) + int(p.get("qty") or 0)
        for sp in (self.bus.get("spreads", {}) or {}).values():
            for leg in sp.get("legs", []):
                sid = str(leg.get("security_id") or "")
                if not sid:
                    continue
                q = int(sp.get("qty") or 0)
                mine[sid] = mine.get(sid, 0) + \
                    (q if leg.get("action") == "BUY" else -q)
        for f in (self.bus.get("futures_positions", {}) or {}).values():
            sid = str(f.get("security_id") or "")
            if sid:
                q = int(f.get("lots") or 0) * int(f.get("lot_size") or 0)
                mine[sid] = mine.get(sid, 0) + \
                    (q if f.get("side") == "LONG" else -q)
        diffs = [(sid, mine.get(sid, 0), broker.get(sid, 0))
                 for sid in sorted(set(broker) | set(mine))
                 if broker.get(sid, 0) != mine.get(sid, 0)]
        for sid, m, b in diffs[:6]:
            self.bus.alert("high", self.name, sid,
                           f"POSITION MISMATCH vs broker: book says {m}, "
                           f"broker says {b} (securityId {sid}) — reconcile "
                           f"manually before trusting any automated exit")
        self.bus.set("broker_reconcile",
                     {"at": time.time(),
                      "checked": len(set(broker) | set(mine)),
                      "mismatches": [{"security_id": s, "ours": m, "broker": b}
                                     for s, m, b in diffs]})

    def _auto_spreads(self):
        """Server-side auto-deployment of enabled strategies. Runs whether
        or not the browser is open. Evaluates every symbol each minute.

        Diagnostic visibility added 2026-07-24: every skip path used to
        be a silent `continue` with no logging at all — meaning if
        bull_put_spread/bear_call_spread simply weren't finding eligible
        setups (wall too close to spot, credit too thin, wrong regime),
        there was no way to tell that apart from "auto-deploy isn't
        running." Same skip-reason-counter pattern already used in
        PriceActionAgent.cycle() for exactly this reason. The full
        evaluate() result (including its own `reasons` list) is also
        stashed per symbol+strategy on the bus so the Strategies page
        can show live "why not eligible right now" text, not just the
        backtest version history."""
        import backtester
        cfg = config.load()
        auto = cfg.get("auto_strategies") or []
        if not auto or not cfg["paper_mode"] or not market_open():
            return
        if time.time() - getattr(self, "_last_auto", 0) < 60:
            return
        self._last_auto = time.time()
        # 2026-07-27 — the portfolio kill-switch's documented 60-minute
        # post-trip cooldown ("New entries are then blocked for
        # portfolio_halt_cooldown_min") was never actually checked
        # here — a real gap found from a live report where fresh
        # spreads appeared to open well inside a cooldown window that
        # had just force-closed everything. The directional signal
        # pipeline already respects this (RiskAgent.evaluate()); this
        # loop simply never looked at the same bus key.
        if time.time() < self.bus.get("portfolio_halt_until", 0):
            self.summary = "auto-deploy paused — portfolio kill-switch cooldown active"
            return
        import strategies as slib
        spreads = self.bus.get("spreads", {}) or {}
        max_sp = cfg.get("max_concurrent_spreads", 2)
        cooldown = cfg.get("spread_reentry_cooldown_min", 15) * 60
        if not hasattr(self, "_spread_cd"):
            self._spread_cd = {}
        skipped = {"no_analysis": 0, "on_cooldown": 0, "not_eligible": 0,
                  "max_concurrent": 0, "entry_failed": 0, "stale_analysis": 0,
                  "consec_loss_halt": 0, "capital_concentration": 0}
        fired = []
        # 2026-07-27 — capital-concentration cap, alongside the existing
        # COUNT-based max_concurrent_spreads: with dynamic sizing on,
        # spreads could keep opening (up to the count limit) as long as
        # margin allowed, potentially committing most of total capital
        # to spreads and leaving little room for directional trades
        # even when they DO clear the regime/confluence gates. This
        # caps the FRACTION of total capital tied up in spread margin,
        # independent of how many individual spreads that represents.
        capital = cfg.get("backtest_capital", 200000)
        margin_per_lot = cfg.get("margin_per_lot_spread", 85000)
        max_spread_capital_pct = cfg.get("max_spread_capital_pct", 60.0)
        # 2026-07-27 — staleness gate added after a live report: a
        # bear_call_spread fired on FINNIFTY while the index was up on
        # the day, prompting the reasonable question "was this decided
        # on delayed data?" Investigation found bear_call_spread is
        # explicitly valid in rangebound/mixed regimes regardless of
        # the day's net direction (REGIME_FIT in strategies.py), so
        # THAT specific case wasn't necessarily wrong — but the
        # investigation surfaced a real, separate gap: this loop read
        # `analysis`/`regime` off the bus with NO freshness check at
        # all, unlike /api/analysis/{symbol}'s own existing "fresh
        # enough" precedent (ts < 90s). A genuinely stale read was
        # never actually ruled out for THIS report, and nothing stopped
        # it from happening on a future one. Gated the same way,
        # same threshold, for consistency.
        stale_after_sec = 90
        for sym in self.bus.get("symbols", []):
            analysis = self.bus.get(f"analysis:{sym}")
            regime = self.bus.get(f"regime:{sym}")
            if not analysis:
                skipped["no_analysis"] += len(auto)
                continue
            chain_age, chain_why = data_age_of(self.bus, f"chain_ts:{sym}",
                                               label=f"{sym} chain data")
            if chain_age is None:
                skipped["stale_analysis"] += len(auto)
                self.bus.log(self.name,
                            f"{sym}: skipping spread evaluation — {chain_why}")
                continue
            if chain_age > stale_after_sec:
                skipped["stale_analysis"] += len(auto)
                self.bus.log(self.name,
                            f"{sym}: skipping spread evaluation — analysis "
                            f"is {chain_age:.0f}s old (> {stale_after_sec}s), "
                            f"not deciding on delayed data")
                continue
            for name in auto:
                if len(spreads) >= max_sp:
                    skipped["max_concurrent"] += 1
                    continue
                # 2026-07-27 — real bug found during a full review pass:
                # spread_margin_deployed was computed ONCE before this
                # loop and never recalculated, even though `spreads`
                # itself DOES get refreshed after each successful entry
                # a few lines below. Within a single cycle, if multiple
                # symbols/strategies were each individually eligible
                # against the snapshot taken at the TOP of the cycle,
                # they could all enter sequentially, each check passing
                # against the SAME stale pre-cycle capital figure —
                # cumulatively exceeding max_spread_capital_pct despite
                # the cap nominally being enforced at every single
                # check. Recomputed fresh from the CURRENT `spreads`
                # dict on every iteration instead of once at the top.
                current_margin_deployed = sum(
                    margin_per_lot * (sp.get("lots") or 1) for sp in spreads.values())
                if capital > 0 and (current_margin_deployed / capital * 100) >= max_spread_capital_pct:
                    skipped["capital_concentration"] += 1
                    continue
                cd_key = f"{sym}:{name}"
                if time.time() - self._spread_cd.get(cd_key, 0) < cooldown:
                    skipped["on_cooldown"] += 1
                    continue
                consec = getattr(self, "_spread_consec_losses", {}).get(cd_key, 0)
                stop_n = cfg.get("spread_stop_after_consecutive_losses", 2)
                if stop_n and consec >= stop_n:
                    skipped["consec_loss_halt"] += 1
                    continue
                # The backtest-profitability gate protects LIVE money —
                # it must never block PAPER auto-deploy, since paper
                # trading is exactly how a strategy earns that proof in
                # the first place. Blocking it here would mean no
                # strategy could ever accumulate enough paper trades to
                # pass the gate.
                if not cfg["paper_mode"] and not backtester.is_live_enabled(name, sym):
                    continue
                ev = slib.evaluate(name, analysis, regime,
                                   candles=self.bus.get(f"regime_candles:{sym}"))
                self.bus.set(f"spread_eval:{sym}:{name}", ev)
                if ev and ev.get("eligible"):
                    r = self.enter_spread(ev)
                    if r.get("ok"):
                        self._spread_cd[cd_key] = time.time()
                        spreads = self.bus.get("spreads", {}) or {}
                        fired.append(f"{sym} {name}")
                    else:
                        skipped["entry_failed"] += 1
                        # 2026-07-28 — real gap found from a live log:
                        # this logged unconditionally every cycle,
                        # producing 596 near-identical lines in one
                        # log file (595 of them "already open on X" —
                        # a persistent condition that doesn't change
                        # cycle to cycle while the existing position
                        # stays open). Doesn't cost money, just
                        # drowns out genuinely new information. Same
                        # rising-edge/periodic-heartbeat pattern
                        # already used elsewhere in this same function
                        # (the "nothing fires" diagnostic breadcrumb
                        # below): log immediately when the reason
                        # actually changes, otherwise at most once
                        # every 10 minutes as a "still blocked" pulse.
                        reason = r.get("error", "unknown reason")
                        if self._should_log_entry_fail(f"{sym}:{name}", reason):
                            self.bus.log(self.name,
                                        f"{sym} {name}: eligible but entry failed "
                                        f"— {reason}")
                else:
                    skipped["not_eligible"] += 1
        self.summary = ("deployed: " + ", ".join(fired)) if fired else \
            f"scanning {len(auto)} auto strategy(ies) across {len(self.bus.get('symbols', []))} symbols ({skipped})"
        # Diagnostic breadcrumb every ~10 min when nothing fires — same
        # cadence/rationale as PriceActionAgent's equivalent breadcrumb.
        if not fired and time.time() - getattr(self, "_last_spread_diag_log", 0) > 600:
            self._last_spread_diag_log = time.time()
            reasons_seen = []
            for sym in self.bus.get("symbols", []):
                for name in auto:
                    ev = self.bus.get(f"spread_eval:{sym}:{name}")
                    if ev and not ev.get("eligible") and ev.get("reasons"):
                        reasons_seen.append(f"{sym}/{name}: {'; '.join(ev['reasons'])}")
            self.bus.log(self.name,
                        f"no spreads deployed this cycle — {skipped}. "
                        + ("latest ineligibility reasons: " + " | ".join(reasons_seen)
                           if reasons_seen else "no eligibility data yet"))

    # ================= defined-risk spreads (PAPER ONLY, phase 1) =========
    def _should_log_entry_fail(self, fail_key, reason):
        """2026-07-28 — extracted for direct testability (see
        test_entry_fail_log_throttle.py). Returns True (and records
        this reason as the new baseline) only when the reason for this
        symbol/strategy pair has genuinely changed since the last log,
        or when at least 10 minutes have passed since the last time
        this exact reason was logged — otherwise returns False,
        silently. A persistent condition like "already open on X"
        would otherwise log identically every single cycle for as
        long as the existing position stays open.

        2026-07-31 — body moved to the module-level
        should_log_throttled() so the futures-OI archive can throttle
        identically instead of growing a second copy of the same rule.
        Behaviour here is unchanged; test_entry_fail_log_throttle.py
        still covers it through this method."""
        return should_log_throttled(self, "_entry_fail_last", fail_key, reason)

    def enter_spread(self, spread):
        """Open a 2-leg credit spread. Refuses in live mode (phase 1)."""
        cfg = config.load()
        if not cfg["paper_mode"]:
            return {"error": "Spreads are paper-mode only in this version. "
                             "Enable Paper mode in Settings to use them."}
        sym = spread["symbol"]
        # Spreads reach here via _auto_spreads() -> enter_spread(), which
        # does NOT pass through RiskAgent.evaluate() (documented hole).
        # The hold therefore has to be enforced here too, or holding a
        # symbol would stop its options and leave its spreads trading.
        if symbol_paused(sym, cfg):
            return {"error": f"{sym} is on hold (paused_symbols)"}
        spreads = self.bus.get("spreads", {}) or {}
        sid = f"{sym}:{spread['name']}:{spread['short_strike']:.0f}"
        if sid in spreads:
            return {"error": f"{spread['name']} already open on {sym} "
                             f"at {spread['short_strike']:.0f}"}
        if len(spreads) >= cfg.get("max_concurrent_spreads", 2):
            return {"error": f"Max concurrent spreads "
                             f"({cfg.get('max_concurrent_spreads', 2)}) reached."}
        lot = cfg["lot_sizes"].get(sym, 75)
        import sizing
        deployed = sizing.deployed_capital(cfg, self.bus.get("positions", {}), spreads)
        n_lots, sizing_why = sizing.size_spread(cfg, sym, spread["max_loss"], deployed)
        self.bus.log(self.name, f"{sym} spread sizing: {sizing_why}")
        if n_lots < 1:
            return {"error": f"Not enough available capital for even 1 lot "
                            f"after existing positions/spreads — {sizing_why}"}
        qty = lot * n_lots
        credit = spread["credit"]
        margin_used = round(cfg.get("margin_per_lot_spread", 85000) * n_lots, 0)
        # 2026-07-27 — dynamic, IV-based profit target, per explicit
        # request. Computed and LOCKED IN at entry time (same as
        # loss_limit already is) rather than recalculated mid-trade —
        # a target that moves under you mid-trade is confusing and
        # makes the exit-reason log meaningless. Reuses data already
        # on the bus (no new fetch): analysis:{sym} for avg_iv,
        # regime:{sym} for the trend-stability check on the elevated
        # band, and history.get_daily_atm_iv_history() for the
        # percentile tier (falls through gracefully if that backfill
        # hasn't been run).
        # 2026-08-06 — prefer the TUNED per-symbol value. profit_capture
        # and loss_mult sat in the tuner's DEFAULT_PARAMS but nothing
        # live ever read them, so every sweep of those two moved
        # backtest numbers only. wall_gap_frac/credit_min_frac were
        # already connected (strategies.evaluate fetches them via
        # get_params), which is what made the gap easy to miss.
        #
        # Their defaults are now SEEDED at what live already used
        # (0.18 / 1.0), so this reads the same numbers today as the
        # config path did. The config keys remain the fallback for any
        # symbol the tuner has no entry for.
        target_pct = cfg.get("spread_profit_target_pct", 30)
        loss_mult = cfg.get("spread_loss_limit_multiple", 1.0)
        target_basis = "fixed (dynamic targets off)"
        try:
            import backtester as _bt
            _tp = _bt.get_params(spread["name"], sym)
            if _tp.get("profit_capture"):
                target_pct = _tp["profit_capture"] * 100.0
                target_basis = f"tuned ({spread['name']}/{sym})"
            if _tp.get("loss_mult"):
                loss_mult = _tp["loss_mult"]
        except Exception:
            pass          # fall back to the config values above
        if cfg.get("dynamic_spread_targets_enabled", False):
            import risk_engine, history as _hist
            an = self.bus.get(f"analysis:{sym}") or {}
            avg_iv = an.get("avg_iv")
            reg = self.bus.get(f"regime:{sym}") or {}
            long_hist = _hist.get_daily_atm_iv_history(sym)
            pctl = (risk_engine.iv_percentile(
                avg_iv, long_hist, f"{len(long_hist)}-day backfilled ATM IV history")
                if avg_iv is not None else None)
            target_pct, target_basis = risk_engine.dynamic_spread_profit_target_pct(
                cfg, avg_iv, pctl, reg.get("regime"), reg.get("adx"))
            self.bus.log(self.name, f"{sym} {spread['name']}: dynamic profit "
                        f"target {target_pct}% of credit — {target_basis}")
        pos = {
            "id": sid, "strategy": spread["name"], "symbol": sym,
            "legs": [dict(l, entry=l["ltp"]) for l in spread["legs"]],
            "qty": qty, "lots": n_lots,
            "credit": credit, "max_loss": spread["max_loss"],
            "margin_used": margin_used,
            "width": spread["width"], "short_strike": spread["short_strike"],
            # Exit thresholds, configurable in Settings. Bug found
            # 2026-07-22: the old fixed 60% profit target NEVER fired —
            # every spread that day rode to EOD square-off with GOT%
            # between -13% and +20% of the target, nowhere close to 60%.
            # A defined-risk credit spread's value decays with theta
            # over its full life to expiry; expecting 60% of that
            # captured within a single session is unrealistic unless
            # there's a large adverse-to-short-side move. Lowered to a
            # target that's actually reachable intraday from time decay
            # + typical moves, with a matching tighter loss cap so the
            # risk:reward isn't stretched into needing an unrealistic
            # win rate to break even.
            "profit_target": round(credit * target_pct / 100, 2),
            "profit_target_pct": target_pct, "profit_target_basis": target_basis,
            # Same as the option case above: the defense zone TIGHTENS
            # loss_limit in place, so the closed record shows the
            # defended limit rather than the one the position was opened
            # against. Preserved below as initial_loss_limit.
            "loss_limit": round(min(credit * loss_mult,
                                    spread["max_loss"]), 2),
            "initial_loss_limit": round(min(
                credit * loss_mult,
                spread["max_loss"]), 2),
            "opened": now_ist().strftime("%H:%M:%S"), "opened_ts": time.time(),
            "pnl": 0.0, "pnl_ts": time.time(), "paper": True, "ai_advice": None, "ai_ts": 0,
        }
        spreads[sid] = pos
        self.bus.set("spreads", spreads)
        self.bus.log(self.name,
                     f"📄 PAPER SPREAD {spread['name']} {sym}: "
                     + " · ".join(f"{l['action']} {l['strike']:.0f} {l['leg']}"
                                  f" @ ₹{l['ltp']}" for l in spread["legs"])
                     + f" · credit ₹{credit} x {qty}")
        self.bus.alert("medium", "execution", sym,
                       f"Spread opened: {spread['name']} credit ₹{credit}")
        return {"ok": True, "spread": pos}

    def _spread_leg_ltp(self, chain, leg):
        row = next((r for r in chain["rows"]
                    if r["strike"] == leg["strike"]), None)
        return row[leg["leg"].lower()].get("ltp") if row else None

    def _monitor_spreads(self):
        cfg = config.load()
        spreads = self.bus.get("spreads", {}) or {}
        if not spreads:
            return
        for sid, sp in list(spreads.items()):
            chain = self.bus.get(f"chain:{sp['symbol']}")
            ltps = ([self._spread_leg_ltp(chain, l) for l in sp["legs"]]
                   if chain else [None] * len(sp["legs"]))
            stale = any(v is None or v == 0 for v in ltps)
            if stale and not market_open():
                # same class of bug as the single-leg fix above: don't
                # let a stale post-close feed block EOD square-off —
                # force it closed using each leg's last known price
                for leg, last in zip(sp["legs"], ltps):
                    if last:
                        leg["ltp"] = last
                pnl_ps = sum((l["entry"] - l["ltp"]) if l["action"] == "SELL"
                            else (l["ltp"] - l["entry"]) for l in sp["legs"])
                sp["pnl"] = round(pnl_ps * sp["qty"], 0)
                sp["pnl_per_share"] = round(pnl_ps, 2)
                self.exit_spread(sid, "market closed — forced square-off (feed stale)")
                continue
            if stale:
                continue
            # v59.69 (third-eye Tier 3) — hold spread exit decisions on
            # a stale chain, same guard as _monitor_one. (Zero-checks
            # above catch a MISSING price; this catches an OLD one.)
            _cts = self.bus.get(f"chain_ts:{sp['symbol']}")
            _max_age = cfg.get("exit_quote_max_age_sec", 90)
            if market_open() and _cts and time.time() - _cts > _max_age:
                if time.time() - getattr(self, "_spr_stale_log_ts", 0) > 60:
                    self._spr_stale_log_ts = time.time()
                    self.bus.log(self.name,
                                 f"{sp['symbol']} chain "
                                 f"{time.time() - _cts:.0f}s old "
                                 f"(>{_max_age}s) — holding spread exit "
                                 f"decisions")
                continue
            # combined P&L per share: SELL leg profits as price falls
            pnl_ps = 0.0
            for leg, ltp in zip(sp["legs"], ltps):
                leg["ltp"] = ltp
                d = (leg["entry"] - ltp) if leg["action"] == "SELL" \
                    else (ltp - leg["entry"])
                pnl_ps += d
            sp["pnl"] = round(pnl_ps * sp["qty"], 0)
            sp["pnl_per_share"] = round(pnl_ps, 2)
            sp["pnl_ts"] = time.time()   # v59.70 — freshness stamp
            sp["mfe"] = max(sp.get("mfe", 0), sp["pnl"])
            sp["mae"] = min(sp.get("mae", 0), sp["pnl"])
            spot = chain.get("spot")
            # ONE definition, shared with backtester.replay_spreads —
            # see spread_exit_reason()'s docstring for why the two
            # drifting apart made every spread backtest meaningless.
            reason = spread_exit_reason(
                sp, pnl_ps, spot, cfg, time.time(), market_open(),
                log=lambda m: self.bus.log(self.name, m),
                alert=lambda lvl, sym, msg: self.bus.alert(lvl, self.name, sym, msg))
            spreads[sid] = sp
            self.bus.set("spreads", spreads)
            # v59.0 Phase D — SHADOW ONLY. Observes what a delta hedge
            # would have done against this real spread. Placed here, at
            # the end of the cycle, so `reason` is already decided and
            # the parent-close case can be measured rather than guessed.
            # Wrapped because an observer must never be able to stop a
            # spread from exiting.
            try:
                self._fhedge_observe(sp, sid, spot, chain, cfg, reason)
            except Exception as e:
                why = f"⚠ hedge shadow failed: {type(e).__name__}: {e}"
                if should_log_throttled(self, "_fhedge_warn", "err", why):
                    self.bus.log(self.name, why)
            if reason:
                self.exit_spread(sid, reason)
            else:
                self._spread_ai_check(sp, chain)

    def _fhedge_observe(self, sp, sid, spot, chain, cfg, parent_reason):
        """Record what the futures delta hedge WOULD have done. No orders.

        v59.0 Phase D. Reads the live spread; writes only to the shadow
        journal. `futures_strategy_enabled` and `futures_live_enabled`
        are irrelevant here because nothing is placed either way — but
        the module is still gated on its own switch so it can be turned
        off without touching spread logic.
        """
        import fhedge_shadow as fh
        if not cfg.get("fhedge_shadow_enabled", True):
            return
        state = getattr(self, "_fhedge_state", None)
        if state is None:
            state = self._fhedge_state = {}
        cur = state.get(sid)

        exp = (chain or {}).get("expiry")
        dte = None
        if exp:
            try:
                import datetime as _dt
                dte = (_dt.date.fromisoformat(str(exp)[:10])
                       - now_ist().date()).days
            except (ValueError, TypeError):
                dte = None
        buf = fh.buffer_points(sp, cfg)
        bp = fh.breach_points(sp, spot)
        unwound, unwind_why, margin = fh.would_unwind(sp, spot, cfg)

        base = {"sid": sid, "symbol": sp.get("symbol"),
                "strategy": sp.get("strategy"), "spot": spot,
                "short_strike": sp.get("short_strike"),
                "breach_pts": (round(bp, 2) if bp is not None else None),
                "buffer_pts": round(buf, 2)}

        # --- the parent closed: the dangerous case ----------------------
        if parent_reason:
            if cur:
                rec = dict(base, event="parent_close", hedge_active=True,
                           parent_reason=parent_reason,
                           hedge_lots=cur.get("lots"),
                           opened_ts=cur.get("opened_ts"),
                           held_seconds=round(time.time() - cur.get("opened_ts", 0)),
                           independently_unwound=unwound,
                           independent_reason=unwind_why,
                           # The invariant: the hedge closes in the SAME
                           # cycle the parent does. In shadow that is
                           # true by construction — this field exists so
                           # the journal proves it per cycle rather than
                           # asserting it once in a test.
                           hedge_closed_same_cycle=True,
                           margin=margin)
                fh.write(rec)
                if not unwound:
                    # The forced-close rule was the ONLY thing preventing
                    # a naked future in this cycle. Say so out loud.
                    gap = margin.get("reclaim_gap_pts")
                    self.bus.log(self.name,
                                 f"[hedge shadow] {sp.get('symbol')} {sid}: parent "
                                 f"closed ({parent_reason}) with hedge OPEN "
                                 f"{cur.get('lots')} lots — would NOT have unwound "
                                 f"on its own (still {gap}pts from reclaim, "
                                 f"{margin.get('minutes_to_eod')}m to EOD). Forced "
                                 f"close is load-bearing here.")
                state.pop(sid, None)
            else:
                fh.write(dict(base, event="parent_close", hedge_active=False,
                              parent_reason=parent_reason, margin=margin))
            return

        # --- unwind on its own rules ------------------------------------
        if cur:
            if unwound:
                fh.write(dict(base, event="unwind", reason=unwind_why,
                              hedge_lots=cur.get("lots"),
                              opened_ts=cur.get("opened_ts"),
                              held_seconds=round(time.time() - cur.get("opened_ts", 0)),
                              margin=margin))
                state.pop(sid, None)
            return

        # --- trigger ----------------------------------------------------
        if bp is None or bp < buf:
            return                       # not breached past the buffer
        # v59.0 item 28 — a hedge on a spread too small to need one is a
        # directional futures position, not risk reduction. Checked BEFORE
        # the delta solve so the journal records the refusal explicitly
        # rather than showing nothing at all.
        _psz_ok, _psz_why = fh.parent_lots_ok(sp, cfg)
        if not _psz_ok:
            fh.write(dict(base, event="trigger_blocked",
                          parent_lots=sp.get("lots"), why=_psz_why))
            return
        nd = fh.net_delta(sp, spot, chain, cfg, dte)
        lot = (cfg.get("lot_sizes") or {}).get(sp.get("symbol"), 0)
        if nd is None:
            # Refuse to size off a delta nobody computed. Logged, not
            # silently skipped — an empty journal must never be able to
            # mean "IV would not solve".
            fh.write(dict(base, event="trigger_blocked", dte=dte,
                          why="net delta unavailable (IV did not solve)"))
            return
        lots, capped, ratio = fh.hedge_lots(nd, lot, cfg)
        if lots < 1:
            fh.write(dict(base, event="trigger_blocked", net_delta=round(nd, 1),
                          over_hedge_ratio=ratio, parent_lots=sp.get("lots"),
                          why=f"delta {nd:.0f} < 1 lot ({lot}) — floored to zero"))
            return
        side = "SHORT" if nd > 0 else "LONG"
        state[sid] = {"lots": lots, "side": side, "opened_ts": time.time()}
        fh.write(dict(base, event="trigger", net_delta=round(nd, 1),
                      hedge_side=side, hedge_lots=lots, lot_size=lot,
                      over_hedge_ratio=ratio, parent_lots=sp.get("lots"),
                      capped=capped, dte=dte, margin=margin))
        self.bus.log(self.name,
                     f"[hedge shadow] {sp.get('symbol')} {sp.get('strategy')}: spot "
                     f"{spot:.0f} breached short strike {sp.get('short_strike'):.0f} "
                     f"by {bp:.0f}pts — WOULD hedge {side} {lots} lot(s) "
                     f"(net delta {nd:.0f}). Nothing placed.")

    def _market_move_context(self, sym):
        """2026-07-28 — per explicit request: AI advisories for open
        positions (spread/option/future) should factor in "the next
        possible market move", not just the position's own static
        numbers (credit, loss limit, entry/SL/target). Reuses the
        SAME regime/MTF-confluence read every directional strategy
        gate already runs on (regime:{sym} — 14 other call sites
        already depend on it) — no new analysis engine, just handing
        an existing read to the LLM as context. Returns a short plain-
        English string, or a clear "no regime data yet" note rather
        than silently omitting this from the prompt."""
        r = self.bus.get(f"regime:{sym}")
        if not r:
            return "no regime/momentum data available this cycle"
        return (f"regime {r.get('regime', '?')} (confidence {r.get('confidence', '?')}%), "
                f"MTF confluence {r.get('confluence', '?')}, "
                f"session change {r.get('session_change_pct', '?')}%, "
                f"ADX {r.get('adx', '?')}, "
                f"directionally allows {r.get('allowed_signals', [])}")

    def _spread_ai_check(self, sp, chain):
        """Periodic LLM advisory for the open spread (HOLD/EXIT + why).
        By default this is advisory only — rule exits (profit target,
        loss limit, time stop, breach, spread defense) remain the only
        thing that actually closes a spread. Found 2026-07-22: this was
        confusing in practice — the AI would confidently analyze a
        position and say "EXIT, 85%, because X" every 5 minutes, but
        nothing ever happened with that beyond a passive alert, which
        looked like the AI was "just watching" and doing nothing.
        `spread_ai_auto_exit_enabled` (Settings) lets a confident EXIT
        call actually close the spread — off by default so this stays
        the same conservative advisory-only behavior unless turned on."""
        cfg = config.load()
        if cfg.get("ai_engine", "local") == "off":
            return
        _risk = (sp.get("loss_limit", 0) or 0) * (sp.get("qty") or 0)
        _trig = ai_advisory_due(sp, cfg, sp.get("pnl", 0), _risk)
        if not _trig:
            return
        sp["ai_ts"] = time.time()
        sp["ai_last_pnl"] = sp.get("pnl", 0)
        _t0 = time.time()
        _do_exit = None   # decided in the try, EXECUTED after it (v59.71)
        try:
            import llm, json as _json
            market_ctx = self._market_move_context(sp["symbol"])
            prompt = (
                "You monitor an open Indian index option credit spread. "
                "Reply ONLY JSON: {\"advice\":\"HOLD|EXIT\",\"confidence\":0-100,"
                "\"why\":\"<15 words\"}.\n"
                f"Strategy: {sp['strategy']} on {sp['symbol']}. "
                f"Short strike {sp['short_strike']}, spot {chain.get('spot')}, "
                f"credit taken {sp['credit']}, current P&L/share "
                f"{sp.get('pnl_per_share', 0)}, profit target "
                f"{sp['profit_target']}, loss limit {sp['loss_limit']}. "
                f"Market context (factor in where price may move next, "
                f"not just the position's own numbers): {market_ctx}.")
            text, engine, err = llm.generate_json(prompt, max_tokens=120)
            if err or not text:
                sp["ai_advice"] = None if err == "ai_off" else f"AI unavailable ({err})"
                return
            j = _json.loads(text)
            if j and j.get("advice"):
                sp["ai_advice"] = (f"{j['advice']} ({j.get('confidence', '?')}%)"
                                   f" — {j.get('why', '')} · {engine}")
                confidence = int(j.get("confidence", 0))
                threshold = cfg.get("spread_ai_exit_confidence_threshold", 75)
                _log_ai_advisory(self, sp["symbol"], "spread", j, confidence,
                                 threshold, cfg, sp.get("pnl", 0),
                                 _trig, time.time() - _t0)
                if j["advice"] == "EXIT" and confidence >= threshold:
                    why = j.get("why", "")
                    self.bus.alert("medium", "execution", sp["symbol"],
                                   f"AI suggests exiting {sp['strategy']}: {why}")
                    # AI Sell marker, per explicit request for AI Buy/
                    # AI Sell Series Markers. HONEST GAP: there is no
                    # "AI Buy" equivalent wired here — this advisory
                    # system only ever runs HOLD/EXIT checks on
                    # ALREADY-OPEN spread positions (see this method's
                    # own docstring); it never recommends new entries,
                    # so no genuine "AI Buy" event exists anywhere in
                    # this codebase to record. Recording the advisory
                    # regardless of whether spread_ai_auto_exit_enabled
                    # actually acts on it — the marker represents the
                    # AI's call, not necessarily an executed trade.
                    self._record_chart_event(sp["symbol"], "ai_sell",
                                             chain.get("spot"),
                                             f"AI EXIT {sp['strategy']} ({confidence}%): {why}")
                    if cfg.get("spread_ai_auto_exit_enabled", False):
                        self.bus.log(self.name,
                                     f"AI auto-exit ENABLED — closing {sp['strategy']} "
                                     f"{sp['symbol']} on AI advisory ({confidence}%): {why}")
                        _do_exit = (sp["id"],
                                    f"AI advisory EXIT ({confidence}%): {why}")
        except Exception as e:
            sp["ai_advice"] = f"AI check unavailable ({e})"
        # v59.71 (third-eye Tier 4) — exit executes OUTSIDE the advice
        # try, so an exit_spread() bug can no longer be relabelled
        # "AI check unavailable" while the spread stays open.
        if _do_exit:
            self.exit_spread(*_do_exit)

    def exit_spread(self, sid, reason="manual exit"):
        spreads = self.bus.get("spreads", {}) or {}
        sp = spreads.get(sid)
        if not sp:
            return {"error": "spread not found"}
        cfg = config.load()
        # fees: per lot per transaction; 2 legs x 2 transactions = 4.
        # premium_out is the spread's exit VALUE via spread_exit_value()
        # — NOT pnl_per_share, which fed the cost model a negative sell
        # notional on losers (v59.68, third-eye Tier 0).
        _c = realistic_costs("option", sp.get("symbol"), sp.get("lots"),
                             sp.get("credit"),
                             spread_exit_value(sp.get("credit"),
                                               sp.get("pnl_per_share", 0)),
                             cfg, legs=2,
                             log=lambda m: self.bus.log(self.name, m))
        fees, slippage = _c["fees"], _c["slippage"]
        warn_zero_fees(self.bus, self.name, "spread", sp.get("lots"), fees)
        gross = sp.get("pnl", 0)
        now = now_ist()
        closed = {
            "symbol": sp["symbol"], "leg": "SPREAD",
            "strike": sp["short_strike"], "qty": sp["qty"],
            "lots": sp["lots"], "strategy": sp["strategy"],
            "source": sp.get("source") or sp.get("strategy"),
            "entry": sp["credit"], "ltp": sp.get("pnl_per_share", 0),
            # 2026-08-02 — `stoploss` used to be written as
            # `-sp["loss_limit"]`. The VALUE was right but the FIELD means
            # a price everywhere else in this codebase, and what was
            # stored is a P&L-per-share floor. Result: 385 of 500 journal
            # rows carried a negative "stop price", which is impossible
            # for anything tradeable, and any consumer doing the obvious
            # `entry - stoploss` arithmetic got nonsense. It produced a
            # bogus "median stop = 200% of premium" in the 2026-08-02 stop
            # study and silently contaminated the per-trade risk figure
            # the options risk cap was calibrated against.
            #
            # A credit spread DOES have a real stop price, in spread-value
            # terms: P&L/share = credit - value, so the loss limit binds
            # when value = credit + loss_limit. That is positive and ABOVE
            # entry, which is correct for a short position. Same for the
            # target: it binds when value = credit - profit_target.
            #
            # The P&L-basis numbers are kept under names that say what
            # they are, so nothing is lost and neither can be misread.
            # Priced off the INITIAL limit: the defense zone may have
            # tightened `loss_limit` mid-life, and sizing was decided
            # against the limit at entry.
            "stoploss": round(sp["credit"]
                              + (sp.get("initial_loss_limit")
                                 or sp["loss_limit"]), 2),
            "initial_loss_limit": sp.get("initial_loss_limit"),
            "target1": round(sp["credit"] - sp["profit_target"], 2),
            "target2": sp["credit"],
            "stop_basis": "spread_value",
            "side": "SHORT",
            "loss_limit_per_share": sp["loss_limit"],
            "profit_target_per_share": sp["profit_target"],
            "closed": now.strftime("%H:%M:%S"),
            "closed_date": now.strftime("%Y-%m-%d"),
            "closed_at": now.isoformat(),
            "opened": sp["opened"], "opened_ts": sp.get("opened_ts"), "paper": True,
            "gross_pnl": gross, "fees": fees, "slippage": slippage,
            "cost_model": _c.get("model"),   # v59.68 — fallback visible in the record
            "mfe": sp.get("mfe", 0), "mae": sp.get("mae", 0),
            "pnl": round(gross - fees - slippage, 0),
            "reason": f"[{sp['strategy']}] {reason}",
        }
        self.bus.log(self.name,
                     f"📄 SPREAD CLOSED {sp['strategy']} {sp['symbol']} — "
                     f"{reason} · gross ₹{gross:.0f} - fees ₹{fees:.0f} "
                     f"- slippage ₹{slippage:.0f} "
                     f"= net ₹{closed['pnl']:.0f}")
        spreads.pop(sid, None)
        self.bus.set("spreads", spreads)
        if not hasattr(self, "_spread_cd"):
            self._spread_cd = {}
        self._spread_cd[f"{sp['symbol']}:{sp['strategy']}"] = time.time()
        # 2026-07-27 — real gap found from a live report: RiskAgent
        # already tracks consecutive losses and halts the DIRECTIONAL
        # signal pipeline after `stop_after_consecutive_losses` (default
        # 2) in a row — but spreads never go through risk.evaluate() at
        # all (_auto_spreads() calls enter_spread() directly), so that
        # circuit breaker never applied to them. A specific pattern in
        # the reported data (bear_call_spread re-selling the SAME
        # FINNIFTY 26,100 CE wall four times in one session, size
        # INCREASING after losses, net -3185 on that one pairing) is
        # exactly what this closes: too many losses in a row on the
        # same (symbol, strategy) pairing now pauses THAT pairing for
        # the rest of the day, same spirit as the existing directional
        # circuit breaker, scoped per-pairing rather than
        # account-wide since a spread's wall-based edge is symbol/
        # strategy specific, not a global signal-quality signal.
        if not hasattr(self, "_spread_consec_losses"):
            self._spread_consec_losses = {}
        pair_key = f"{sp['symbol']}:{sp['strategy']}"
        if closed["pnl"] < 0:
            self._spread_consec_losses[pair_key] = \
                self._spread_consec_losses.get(pair_key, 0) + 1
            stop_n = config.load().get("spread_stop_after_consecutive_losses", 2)
            if stop_n and self._spread_consec_losses[pair_key] >= stop_n:
                self.bus.log(self.name,
                            f"{pair_key}: {self._spread_consec_losses[pair_key]} "
                            f"losses in a row on this wall/strategy — pausing "
                            f"auto-deploy for this pairing for the rest of "
                            f"the day (spread_stop_after_consecutive_losses)")
        else:
            self._spread_consec_losses[pair_key] = 0
        _record_closed(self.bus, closed)   # capped window (v59.71)
        _append_trade(closed)
        self.bus.alert("high", "execution", sp["symbol"],
                       f"Spread closed — {reason} — net ₹{closed['pnl']:.0f}")
        self.bus.publish("closed", closed)
        return {"closed": closed}

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
        """Wrapper: stamp the re-entry cooldown on ANY refusal, not only
        on a completed exit.

        2026-08-06, third occurrence of one pattern in a single session:

          S10 zero-lot   sizing returned 0 lots, re-fired every 5s
          SENSEX churn   opened and closed 5x in 38s
          approve/refuse APPROVED then blocked by the rupee cap, 4x/19s

        All three are the same bug: the signal-handled bookkeeping only
        ever happened on a SUCCESSFUL FILL, so every downstream refusal
        path re-fired for as long as the signal stayed valid. Each pass
        costs a full AI probability evaluation and writes an alert.

        Stamping on refusal closes all three with one mechanism. It can
        only ever REDUCE order flow — a refused order is not becoming an
        accepted one because of this.
        """
        # ATOMICALLY CLAIM THE SYMBOL BEFORE ANY SLOW WORK.
        #
        # 2026-08-06 14:11, observed live:
        #
        #   14:11:18  PAPER BUY 65 x NIFTY 24650 CE @ 150.9   order A
        #   14:11:23  PAPER BUY 65 x NIFTY 24650 CE @ 151.3   order B
        #   14:11:31  B registers into positions["NIFTY"]
        #   14:11:35  A registers — OVERWRITING B
        #
        # 130 qty bought, 65 tracked. The untracked half had no stop, no
        # exit monitoring, and was invisible to concurrent-position
        # counts, deployed_capital and the portfolio kill-switch. No
        # error was logged: `positions` is keyed by SYMBOL, so the
        # second write silently replaced the first.
        #
        # The risk gate's "no open position on X" check is not enough —
        # it is a CHECK-THEN-ACT race. The position does not exist until
        # _place finishes, and that took 13-17 SECONDS that afternoon
        # because the AI probability call was blocking on an Ollama
        # timeout. Normal is ~1s. The window widens exactly when the UI
        # looks dead and a user is most likely to click again, which is
        # what happened.
        #
        # This applies to MANUAL clicks too, deliberately. The re-entry
        # cooldown is a throttle and exempts manual; "one tracked
        # position per symbol" is a CORRECTNESS invariant, and an
        # operator double-clicking a slow button is precisely the case
        # that broke it.
        _sym = job.get("symbol")
        _dsig = (job.get("signal") or {}).get("signal")
        _claimed = False
        if _dsig in ("BUY_CE", "BUY_PE") and _sym:
            with self._entry_lock:
                if _sym in (self.bus.get("positions", {}) or {}):
                    return {"error": f"position already open on {_sym}"}
                if _sym in self._entering:
                    return {"error": f"entry already in progress for {_sym} "
                                     f"— duplicate submission ignored"}
                self._entering.add(_sym)
                _claimed = True
        try:
            res = self._place(job, manual=manual)
        finally:
            if _claimed:
                with self._entry_lock:
                    self._entering.discard(_sym)
        try:
            sig = job.get("signal") or {}
            err = str((res or {}).get("error") or "") if isinstance(res, dict) else ""
            # Do NOT re-stamp when the refusal IS the cooldown, or it
            # would refresh itself forever and never expire.
            if err and "re-entry cooldown" not in err and \
                    sig.get("signal") in ("BUY_CE", "BUY_PE"):
                _leg = "CE" if sig["signal"] == "BUY_CE" else "PE"
                _key = f"{job.get('symbol')}:{sig.get('strike')}:{_leg}"
                _cd = self.bus.get("option_reentry_block") or {}
                _cd[_key] = time.time()
                self.bus.set("option_reentry_block", _cd)
        except Exception:
            # Bookkeeping must never be able to break order placement.
            # (Not a bare swallow of the order path — `res` is already
            # computed and is returned regardless.)
            pass
        return res

    def _place(self, job, manual=False):
        cfg = config.load()
        sig, analysis, sym = job["signal"], job["analysis"], job["symbol"]
        # See the stamp in exit(). Blocks re-entry into the SAME
        # symbol+strike+leg for option_reentry_cooldown_sec. Manual
        # clicks are exempt: an operator re-entering deliberately is a
        # decision, not a loop.
        if not manual and sig.get("signal") in ("BUY_CE", "BUY_PE"):
            _leg = "CE" if sig["signal"] == "BUY_CE" else "PE"
            _key = f"{sym}:{sig.get('strike')}:{_leg}"
            _cool = cfg.get("option_reentry_cooldown_sec", 180)
            _last = (self.bus.get("option_reentry_block") or {}).get(_key, 0)
            _age = time.time() - _last
            if _last and _age < _cool:
                return {"error": f"re-entry cooldown: {_key} closed "
                                 f"{_age:.0f}s ago, need {_cool}s"}
        lot = cfg["lot_sizes"].get(sym, 75)
        import sizing
        deployed = sizing.deployed_capital(
            cfg, self.bus.get("positions", {}), self.bus.get("spreads", {}))
        if sig.get("source") == "mtf_confluence" and sig.get("atr"):
            # rinkoo.docx's exact ATR-based formula for this specific
            # strategy — delta=0.5 since this is an ATM option, not a
            # future (see mtf_confluence_strategy.py / size_by_atr_risk
            # docstrings for the reasoning). Every other strategy keeps
            # the existing risk_pct-based size_option_buy — this hook
            # only fires for signals explicitly tagged mtf_confluence.
            n_lots, sizing_why = sizing.size_by_atr_risk(
                cfg, sym, sig["atr"], delta=0.5, deployed=deployed)
        else:
            n_lots, sizing_why = sizing.size_option_buy(
                cfg, sym, sig["entry"], sig["stoploss"], deployed)
        self.bus.log(self.name, f"{sym} sizing: {sizing_why}")
        # v59.0 (2026-08-02) — PER-TRADE RUPEE CAP ON THE OPTIONS PATH.
        #
        # The futures path got this cap on 2026-08-01; options never had
        # one, so `portfolio_max_drawdown` was the only thing standing
        # between a single option trade and the whole book. Measured
        # against the 500 real option trades in the journal, ONE lot
        # already risks a median ₹3,198 — 64% of the ₹5,000 portfolio
        # cap — with a p90 of ₹4,778 and a maximum of ₹6,435. That last
        # figure is the point: a single trade could exceed the entire
        # portfolio drawdown allowance on its own, which makes the
        # portfolio cap meaningless exactly when it matters.
        #
        # The SAME shared helper as futures, keyed differently. Not a
        # second implementation — a per-trade cap that drifts from the
        # futures one is precisely the two-copies failure this codebase
        # keeps re-learning.
        #
        # NOTE ON WHAT THIS CAN AND CANNOT DO: at lots_per_trade=1 there
        # is nothing to size down TO, so the cap can only block. It is
        # therefore set at the portfolio cap rather than tighter — the
        # motivated line is "no single trade may consume the entire
        # portfolio allowance", which blocks ~8% of historical trades. A
        # tighter cap (₹2,500, matching futures) would block 69% of them,
        # and that is not a risk control, it is a shutdown. Tightening
        # further requires narrowing the STOPS first, which is a strategy
        # change, not a risk-layer change.
        _cap_lots, _cap_why = sizing.cap_by_rupee_risk(
            cfg, sym, sig["entry"], sig["stoploss"], n_lots,
            key="option_risk_per_trade_rupees")
        if _cap_why:
            self.bus.log(self.name, f"🛡 {sym} option risk cap: {_cap_why}")
        if _cap_lots < 1:
            self.bus.alert("medium", self.name, sym,
                           f"option trade refused — {_cap_why}")
            return {"error": f"per-trade risk cap: {_cap_why}"}
        n_lots = _cap_lots
        if n_lots < 1:
            self.bus.log(self.name, f"⚠ {sym} order skipped — not enough "
                                    f"available capital for even 1 lot")
            return {"error": "insufficient available capital"}
        qty = lot * n_lots
        leg = "CE" if sig["signal"] == "BUY_CE" else "PE"
        label = f"{sym} {sig['strike']} {leg}"
        # FILL AT THE LIVE PRICE, NOT THE ANALYSIS-PACK PRICE.
        #
        # 2026-08-06. `sig["option_ltp"]` is copied out of
        # analysis["strikes"] by analyzer._attach_security_id, and
        # analysis:{sym} is written by TechnicalAgent at interval=60.
        # The EXIT check three hundred lines below reads
        # bus.get(f"chain:{sym}"), which MarketDataAgent refreshes every
        # 3s. Entry and exit therefore ran off feeds 20x apart in
        # cadence, so a fill could be priced up to a minute stale while
        # its own first exit check was 3 seconds fresh.
        #
        # That is what produced the SENSEX 78700 CE loop: five entries
        # inside one 60s analysis window all took the SAME ₹358.85 while
        # the live chain moved 363.00 -> 366.05, and each one satisfied
        # its target immediately. Reading the same chain both sides
        # removes the asymmetry at the source; the target2 repair in
        # analyzer and the re-entry cooldown below are the other two
        # independent breaks in that loop.
        _chain = self.bus.get(f"chain:{sym}")
        _row = (next((r for r in _chain["rows"]
                      if r["strike"] == sig["strike"]), None)
                if _chain and _chain.get("rows") else None)
        _live = (_row.get(leg.lower()) or {}).get("ltp") if _row else None
        fill = _live or sig.get("option_ltp") or sig["entry"]
        if _live and sig.get("option_ltp") and sig["option_ltp"] > 0:
            _drift = abs(_live - sig["option_ltp"]) / sig["option_ltp"]
            if _drift > 0.02:
                self.bus.log(self.name,
                             f"{label}: filling at LIVE ₹{_live} — signal "
                             f"priced ₹{sig['option_ltp']} ({100*_drift:.1f}% "
                             f"stale from the {TechnicalAgent.interval}s "
                             f"analysis pack)")
        # REFUSE A POSITION THAT WOULD EXIT ON ITS FIRST MONITOR CYCLE.
        #
        # 2026-08-06, fourth instance of one family today. SENSEX 78800
        # CE opened and closed in the SAME SECOND at 11:43:02 on "spot
        # invalidation (78756 vs 78791.6)" — live spot was already past
        # the signal's own invalidation level BEFORE the position
        # existed, because that level came from the 60s analysis pack
        # while the exit check reads the 3s chain.
        #
        # Same shared predicate _monitor_one uses, against the same live
        # chain the fill above is priced from, so entry and exit cannot
        # disagree about whether the trade is already over.
        # `entry`/`initial_sl` are deliberately LEFT OUT of the probe.
        # They exist only to label a stop hit as a TRAIL ("locked above
        # entry"), and at entry time there is no trail — a stop above
        # the fill is a degenerate signal, not a banked profit. Passing
        # them made a refusal read "trailing stop in profit ... locked
        # above entry ₹200.0", which is nonsense for a position that
        # never opened. Omitting them yields the honest plain label.
        _probe = {"ltp": fill, "leg": leg,
                  "stoploss": sig["stoploss"], "target2": sig["target2"],
                  "t1_hit": False,
                  "spot_invalidation": sig.get("spot_invalidation")}
        _already = instant_exit_reason(
            _probe, fill, (_chain or {}).get("spot"))
        if _already:
            self.bus.log(self.name,
                         f"{label}: entry REFUSED — would exit immediately: "
                         f"{_already}")
            self.bus.alert("medium", self.name, sym,
                           f"entry refused — already at exit: {_already}")
            return {"error": f"would exit immediately: {_already}"}
        # v59.69 (third-eye Tier 3) — ORDER OF OPERATIONS fixed. This
        # used to run: place live order → build position dict → check
        # duplicates. Two live-money defects in that sequence: a
        # KeyError while building the dict AFTER a live fill left a
        # live, untracked, un-stopped position (the literal reads
        # sig["strike"]/sig["target1"]/… unguarded), and the duplicate
        # check could "abandon" an entry whose order had ALREADY gone
        # to the exchange. Now: duplicate check → build the position →
        # place the order → register. The position exists locally
        # before any money moves.
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            self.bus.log(self.name,
                         f"⚠ {label}: entry ABANDONED — a position on {sym} "
                         f"already exists (opened {positions[sym].get('opened')}). "
                         f"Refusing to overwrite it.")
            self.bus.alert("high", self.name, sym,
                           f"duplicate entry abandoned — {sym} already open")
            return {"error": f"position already open on {sym} — not overwritten"}
        pos = {
            "symbol": sym, "strike": sig["strike"], "leg": leg, "qty": qty,
            "lots": n_lots,
            "entry": fill, "stoploss": sig["stoploss"],
            # 2026-08-02 — `stoploss` is RATCHETED IN PLACE by the trail
            # and the breakeven lock, so by the time a trade closes it is
            # the FINAL stop, not the one sizing was decided against.
            # Futures have carried `initial_sl` for exactly this reason;
            # options did not, so every per-trade risk figure derived
            # from the option journal was measuring the trailed stop and
            # therefore UNDERSTATING entry risk. It is why 5% stop widths
            # appear across all four symbols in the history: those are
            # trailed stops, not entry stops. Never mutated.
            "initial_sl": sig["stoploss"],
            "target1": sig["target1"], "target2": sig["target2"],
            "spot_invalidation": sig.get("spot_invalidation"),
            "security_id": sig.get("security_id"), "order_id": None,
            "opened": now_ist().strftime("%H:%M:%S"), "opened_ts": time.time(), "ltp": fill,
            # Strategy 7 (v51): setup tag + candle-time entry stamp for
            # the structure-break exit (pivots are keyed on CANDLE time,
            # not wall-clock, so opened_ts alone would compare the wrong
            # clock). entry_ts = last 1m bar's time at entry.
            "setup": sig.get("setup") or sig.get("source"),
            "entry_ts": ((self.bus.get(f"pa_candles:{sym}") or {}).get("c1") or [{}])[-1].get("time"),
            "s7_gates": sig.get("s7_gates"),
            "pnl": 0.0, "pnl_ts": time.time(), "t1_hit": False, "paper": cfg["paper_mode"],
            "manual": manual, "capital_used": round(fill * qty, 0),
            # AI Probability Engine (Feature #8) — decision-context
            # snapshot at ENTRY time, needed to bucket this trade's
            # eventual outcome for future probability estimates. Not
            # used by anything at entry itself — flows through
            # unchanged into the closed-trade record since exit()
            # copies ALL of pos's fields (dict(p, closed=..., ...)),
            # so this is genuinely free to add here.
            "entry_confidence": sig.get("confidence"),
            "entry_institutional_agreement":
                (sig.get("ai_decision") or {}).get("institutional_agreement"),
            "entry_technical_agreement":
                (sig.get("ai_decision") or {}).get("technical_agreement"),
            "entry_regime": (self.bus.get(f"regime:{sym}") or {}).get("regime"),
            # Dynamic Risk Monitoring (Feature #9) — additional entry
            # snapshots needed to detect DEGRADATION since entry (a
            # gamma spike or liquidity collapse only means something
            # relative to what it looked like when the trade was
            # opened). Reuses the SAME chain row already being read
            # for this entry, no extra fetch.
            "entry_gamma": None,
            "entry_liquidity_score": None,
        }
        try:
            entry_chain = self.ctx.get("get_chain", lambda s: None)(sym)
            entry_row = next((r for r in (entry_chain or {}).get("rows", [])
                             if r.get("strike") == sig["strike"]), None)
            if entry_row:
                entry_leg_data = entry_row.get(leg.lower()) or {}
                pos["entry_gamma"] = entry_leg_data.get("gamma")
                # (an unused `import risk_engine` sat here inside this
                # swallow — a broken import would have been permanently
                # invisible. Removed, v59.71.)
                bid, ask = entry_leg_data.get("bid"), entry_leg_data.get("ask")
                if bid and ask and ask > bid:
                    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
                    pos["entry_liquidity_score"] = min(100, round(spread_pct * 50))
        except Exception:
            pass
        if cfg["paper_mode"]:
            pos["order_id"] = paper_order_id()
            self.bus.log(self.name, f"📄 PAPER BUY {qty} x {label} @ ₹{fill}")
        else:
            orders = self.ctx["orders_factory"]()
            if orders is None or not sig.get("security_id"):
                self.bus.log(self.name, "⚠ cannot place live order "
                                        "(no broker / security_id)")
                return
            try:
                resp = orders.place(sym, sig["security_id"], "BUY", qty,
                                    "MARKET")
                # "UNCONFIRMED", not "?": a 200 with an unexpected body
                # is a REAL order whose id we failed to read — the
                # record must say that, not shrug.
                pos["order_id"] = resp.get("orderId") or "UNCONFIRMED"
                pos["order_status"] = self._confirm_order(
                    orders, resp, f"BUY {sym} {sig.get('strike')} {leg}")
                # v59.72 (R2 finding H2) — ACT on the verdict. A rejected
                # BUY means no fill exists; registering the position
                # anyway created a phantom that the exit path would later
                # SELL — an unintended naked short.
                if pos["order_status"] in ("REJECTED", "CANCELLED"):
                    self.bus.log(self.name,
                                 f"LIVE BUY {pos['order_status']} at the "
                                 f"broker — entry abandoned, nothing tracked")
                    return {"error": f"live BUY {pos['order_status']} at "
                                     f"the broker (see alert)"}
                # v59.75 — book the REAL fill, not the pre-trade quote,
                # and record the measured entry slippage alongside it.
                _fpx, _ = self._actual_fill(orders, resp, qty,
                                            f"BUY {sym} {sig.get('strike')}")
                if _fpx:
                    pos["quote_at_entry"] = fill
                    pos["entry_fill_slippage"] = round(_fpx - fill, 2)
                    pos["entry"] = _fpx
                    pos["ltp"] = _fpx
                    pos["capital_used"] = round(_fpx * qty, 0)
            except Exception as e:
                # A timeout does NOT mean the order failed — it may have
                # reached the exchange after this call gave up. Track
                # the position as open and demand a manual check;
                # believing we are flat while the broker holds a fill is
                # the unrecoverable direction.
                pos["order_id"] = "UNCONFIRMED-ERROR"
                self.bus.alert("high", self.name, sym,
                               f"LIVE BUY status UNKNOWN "
                               f"({type(e).__name__}: {e}) — position "
                               f"tracked as open; verify at the broker NOW")
            self.bus.log(self.name, f"🔴 LIVE BUY {qty} x {label} — "
                                    f"order {pos['order_id']}")
        # Defence in depth behind place()'s claim: re-read in case a
        # position for this symbol appeared while the order was in
        # flight. A silent overwrite is how 65 qty went untracked on
        # 2026-08-06; an error is recoverable, a phantom position is not.
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            self.bus.alert("high", self.name, sym,
                           f"RACE: a {sym} position appeared while the order "
                           f"was in flight — new order {pos['order_id']} is "
                           f"NOT tracked; verify at the broker")
            return {"error": f"position already open on {sym} — not overwritten"}
        positions[sym] = pos
        self.bus.set("positions", positions)
        self.bus.set("position", pos)   # legacy single-position mirror (most-recent)
        self.bus.set("trades_today", self.bus.get("trades_today", 0) + 1)
        self.bus.set("pending_confirmation", None)
        self.bus.alert("high", "execution", sym,
                       f"{'PAPER' if cfg['paper_mode'] else 'LIVE'} entry: "
                       f"BUY {qty} x {label} @ ₹{fill}")
        # Series Markers, per explicit request (Buy/Sell/Entry/Exit/AI
        # Buy/AI Sell/Target Hit/Stop Loss Hit) — records a real trade
        # LIFECYCLE event with a genuine timestamp and the underlying's
        # SPOT price at that moment (the chart shows spot candles, not
        # option premium, so markers need to be placed on the spot
        # scale) rather than the anchor_ts workaround the earlier
        # current-state-flag markers (smart money/institutional) needed
        # — these are real historical events with their own real time,
        # not a recomputed-every-cycle snapshot.
        self._record_chart_event(sym, "entry", job["analysis"].get("spot"),
                                 f"BUY {leg} {sig['strike']} @ ₹{fill}")

    def _record_chart_event(self, symbol, kind, spot, label):
        """Appends a chart marker event to chart_events:{symbol} — a
        capped (last 50) list of real trade lifecycle events, read by
        /ws/candles to build Series Markers. `kind` is one of "entry",
        "exit", "target_hit", "stop_hit" (exit's own reason string is
        classified into the latter two where applicable, generic
        "exit" otherwise). Silently no-ops if spot is unavailable
        (e.g. chain fetch failed) rather than placing a marker at a
        nonsensical price."""
        if spot is None:
            return
        events = self.bus.get(f"chart_events:{symbol}", [])
        events.append({"time": int(time.time()), "kind": kind,
                       "spot": spot, "label": label})
        self.bus.set(f"chart_events:{symbol}", events[-50:])

    def _monitor(self):
        positions = self.bus.get("positions", {}) or {}
        if not positions:
            self.summary = self.summary or "idle"
            return
        summaries = []
        for sym, p in list(positions.items()):
            # v59.71 (third-eye Tier 4) — per-position isolation: one
            # malformed position used to kill monitoring of every OTHER
            # open position in the same cycle. The broken one alerts
            # (throttled per symbol) — an unmonitorable position means
            # its stop is not being enforced, which is HIGH by any
            # definition — and the rest keep their protection.
            try:
                r = self._monitor_one(p)
            except Exception as e:
                _ts_map = getattr(self, "_mon_err_ts", {})
                self._mon_err_ts = _ts_map
                if time.time() - _ts_map.get(sym, 0) > 300:
                    _ts_map[sym] = time.time()
                    self.bus.alert("high", self.name, sym,
                                   f"position monitor CRASHED for {sym} "
                                   f"({type(e).__name__}: {e}) — its stop/"
                                   f"target is NOT being enforced")
                summaries.append(f"{sym} monitor error ({type(e).__name__})")
                continue
            if r:
                summaries.append(r)
        self.summary = " · ".join(summaries) if summaries else "idle"

    def _monitor_one(self, p):
        cfg = config.load()
        sym = p["symbol"]
        # (Strategy 7's structure-break exit moved BELOW the market-
        # closed and stale-quote guards in v59.72 — R2 finding M7: it
        # used to fire ~40 lines before them, so an S7 position could
        # still exit at 23:00 or off a 300s-old chain.)
        chain = self.bus.get(f"chain:{sym}")
        row = (next((r for r in chain["rows"] if r["strike"] == p["strike"]), None)
              if chain else None)
        ltp = row[p["leg"].lower()].get("ltp") if row else None
        if not ltp and not market_open():
            # Market is closed AND the feed has nothing fresh — this is
            # exactly the failure mode that left a position stuck open
            # overnight: the old code returned early on "no LTP" before
            # ever reaching the EOD check below. Force the square-off
            # using the last successfully recorded price rather than
            # waiting forever for a quote that will never arrive once
            # the session has ended.
            ltp = p.get("ltp") or p.get("entry")
            self.exit(f"market closed — forced square-off (feed stale, "
                     f"last known price ₹{ltp})", symbol=sym)
            return f"{sym} {p['strike']} {p['leg']} — forced EOD close (stale feed)"
        if not chain or not row:
            return None
        if not ltp:
            # Dhan sometimes returns no/0 LTP for a strike in a given
            # snapshot — skip this cycle rather than comparing None
            return f"{sym} {p['strike']} {p['leg']} — no LTP; retrying"
        # --- v59.69 (third-eye Tier 3) exit-decision guards ------------
        # 1. Market closed: the ONLY legitimate action is the square-off.
        #    The EOD branch used to sit at the BOTTOM of the exit chain,
        #    so after-hours remnant quotes kept the stop/target/ratchet
        #    chain live — spreads "hit profit target" at 23:52 on
        #    2026-07-30 through the same pattern. Value at the last price
        #    and close; this subsumes the old bottom-of-chain EOD branch.
        if not market_open():
            p["ltp"] = ltp
            p["pnl"] = round((ltp - p["entry"]) * p["qty"], 0)
            positions = self.bus.get("positions", {}) or {}
            if sym in positions:
                positions[sym] = p
                self.bus.set("positions", positions)
            self.exit("market closing — squaring off intraday position",
                      symbol=sym)
            return f"{sym} {p['strike']} {p['leg']} — EOD square-off"
        # 2. Stale quote: acting on an old price during a fast move is
        #    worse than not acting (MarketDataAgent's failure backoff
        #    reaches 300s). data_age_of() guarded the ENTRY gates only;
        #    exits had no age check at all. Hold decisions — and don't
        #    write the stale price into pnl/mfe/mae or the trail ratchet,
        #    or the kill-switch sums it as if current.
        _cts = self.bus.get(f"chain_ts:{sym}")
        _max_age = cfg.get("exit_quote_max_age_sec", 90)
        if _cts and time.time() - _cts > _max_age:
            return (f"{sym} {p['strike']} {p['leg']} — quote "
                    f"{time.time() - _cts:.0f}s old (>{_max_age}s) — "
                    f"holding exit decisions until fresh data")
        # Strategy 7 (v51) — structure-break exit, spec exit rule #3:
        # a NEW confirmed ZigZag pivot printing adverse structure
        # (LH/LL while long, HH/HL while short) closes the position —
        # structure invalidation, mirroring the spot-invalidation rule.
        # Pivots come from the SAME zigzag on the SAME pa_candles 1m
        # series the entry gate used (parity). Only pivots confirmed
        # AFTER entry count. Runs BELOW the guards as of v59.72 (R2 M7).
        if p.get("setup") == "sg_ema" and p.get("entry_ts"):
            pack = self.bus.get(f"pa_candles:{sym}")
            if pack and pack.get("c1"):
                import structure
                confirmed = [pv for pv in structure.zigzag_series(pack["c1"])
                             if pv.get("structure")
                             and pv.get("time", 0) > p["entry_ts"]]
                if confirmed:
                    last = confirmed[-1]["structure"]
                    is_long = p.get("leg", "").upper() == "CE" or \
                              "CE" in str(p.get("signal", ""))
                    adverse = (last in ("LH", "LL")) if is_long else \
                              (last in ("HH", "HL"))
                    if adverse:
                        return self.exit(f"structure break ({last} pivot "
                                        f"confirmed after entry)", symbol=sym)
        p["ltp"] = ltp
        p["pnl"] = round((ltp - p["entry"]) * p["qty"], 0)
        p["pnl_ts"] = time.time()   # v59.70 — freshness stamp; the kill-switch checks it
        p["mfe"] = max(p.get("mfe", 0), p["pnl"])
        p["mae"] = min(p.get("mae", 0), p["pnl"])
        spot = chain["spot"]
        summary = f"{sym} {p['strike']} {p['leg']} ₹{ltp} P&L ₹{p['pnl']:.0f}"
        cfg = config.load()

        # Dynamic Risk Monitoring (Feature #9) — per explicit request:
        # options markets move fast, so risk needs continuous
        # reassessment against ENTRY-time conditions, not just a check
        # at open. Runs every 2s alongside the existing price-based
        # exit logic below (same cycle, no extra polling). Only
        # SURFACES suggestions (alert + bus key) — never auto-executes
        # a reduce/exit itself, matching this whole feature's human-
        # in-the-loop principle; a future config toggle could act on
        # "high" severity automatically, not added yet.
        if cfg.get("dynamic_risk_monitoring_enabled", True):
            try:
                import risk_engine as rengine
                institutional = self.bus.get(f"institutional:{sym}")
                technical = self.bus.get(f"technical:{sym}")
                analysis = self.bus.get(f"analysis:{sym}")
                smart_money = (analysis or {}).get("smart_money")
                events = rengine.dynamic_risk_check(p, row, institutional,
                                                    technical, smart_money)
                self.bus.set(f"dynamic_risk:{sym}", events)
                prev_signals = set(getattr(self, "_last_dynamic_signals", {}).get(sym, []))
                current_signals = {e["signal"] for e in events}
                new_signals = current_signals - prev_signals
                if new_signals:
                    if not hasattr(self, "_last_dynamic_signals"):
                        self._last_dynamic_signals = {}
                    self._last_dynamic_signals[sym] = current_signals
                    for e in events:
                        if e["signal"] in new_signals:
                            self.bus.alert(
                                "high" if e["severity"] == "high" else "medium",
                                "execution", sym,
                                f"⚡ {e['signal']} on {sym} {p['strike']} {p['leg']} — "
                                f"suggest {e['suggested_action']}: {e['detail']}")
            except Exception as e:
                self.bus.log(self.name, f"⚠ dynamic risk check failed for {sym}: {e}")

        # Rupee-based step-ratchet trailing (alternative to the %-based
        # trail_sl_* mechanism below) — matches a "when profit reaches X,
        # lock at Y; every further Z gained, raise the floor by W" style
        # ratchet in plain rupees rather than price %. Computed BEFORE
        # the exit-reason chain so a floor raised this cycle is already
        # current when checked for breach this same cycle.
        if cfg.get("step_trail_enabled", False):
            lock_trigger = cfg.get("step_trail_lock_trigger_rupees", 2000)
            lock_profit = cfg.get("step_trail_lock_profit_rupees", 1000)
            step_rupees = cfg.get("step_trail_step_rupees", 1000)
            step_gain = cfg.get("step_trail_step_gain_rupees", 500)
            if p["pnl"] >= lock_trigger:
                floor = lock_profit
                if step_rupees > 0:
                    extra_steps = int((p["pnl"] - lock_trigger) // step_rupees)
                    floor += extra_steps * step_gain
                if floor > p.get("step_floor", 0):
                    p["step_floor"] = floor

        reason = None
        # Evaluated ONCE per cycle (it mutates the ratchet state, so
        # calling it twice inside a chain would double-advance it).
        _rpf_option = rupee_profit_floor(p, p["pnl"], cfg, "option")
        _dyn_exit = dynamic_exit_reason(p, self.bus, cfg)
        txn_sl = cfg.get("transaction_stop_loss_rupees", 0)
        txn_target = cfg.get("transaction_target_rupees", 0)
        if txn_sl > 0 and p["pnl"] <= -abs(txn_sl):
            reason = f"transaction stop loss (₹{p['pnl']:.0f} ≤ -₹{txn_sl:.0f})"
        elif txn_target > 0 and p["pnl"] >= txn_target:
            reason = f"transaction target (₹{p['pnl']:.0f} ≥ ₹{txn_target:.0f})"
        elif p.get("step_floor", 0) > 0 and p["pnl"] <= p["step_floor"]:
            reason = (f"step-trail: gave back to floor ₹{p['step_floor']:.0f} "
                     f"(peak ₹{p.get('mfe', 0):.0f})")
        elif _rpf_option:
            reason = _rpf_option
        elif _dyn_exit:
            # The Pine original's early exit. Sits after the hard
            # stop/target and the profit floor, before the time stop and
            # EOD square-off — it is a strategy-specific edge signal,
            # not a risk backstop, so it must not pre-empt the ones that
            # protect capital.
            reason = _dyn_exit
        # 2026-08-03 — the stop label: the trail and the breakeven lock
        # RATCHET p["stoploss"] upward, so once it sits above entry,
        # hitting it is a PROFIT being banked, not a loss being cut. 8
        # of 34 "stoploss" exits in the journal were profitable, which
        # makes any analysis bucketed by exit reason wrong. Behaviour
        # unchanged; only the label is honest.
        #
        # 2026-08-06 — the three PRICE-LEVEL branches (stop, target-2,
        # spot invalidation) now live in instant_exit_reason() because
        # the ENTRY path has to evaluate the identical conditions to
        # refuse a position that would exit on its first cycle. Two
        # copies would drift; see that function's docstring. The
        # t1_hit branch stays here — it is not an entry condition.
        elif (_instant := instant_exit_reason(p, ltp, spot)):
            reason = _instant
        elif p["t1_hit"] and ltp <= p["entry"]:
            reason = "gave back gains after T1"
        elif cfg.get("time_stop_minutes", 0) and p.get("opened_ts") and \
                (time.time() - p["opened_ts"]) / 60 >= cfg["time_stop_minutes"]:
            elapsed = (time.time() - p["opened_ts"]) / 60
            reason = (f"time stop ({elapsed:.0f}m ≥ {cfg['time_stop_minutes']}m) "
                     f"— neither target nor stop hit, forcing a decision")
        # (the EOD square-off branch moved to the TOP of this function in
        # v59.69 — a closed market must pre-empt the whole chain, not be
        # its last resort)

        if not p["t1_hit"] and ltp >= p["target1"]:
            p["t1_hit"] = True
            p["stoploss"] = max(p["stoploss"], p["entry"])
            self.bus.log(self.name, f"✅ {sym} T1 hit ₹{ltp} — SL trailed to "
                                    f"breakeven ₹{p['stoploss']}")
        # ---- trailing stoploss (independent of T1) ----
        # Once the option moves trigger% above entry, the SL follows the
        # peak price at gap% below it (fixed_pct mode) OR at an
        # ATR-scaled distance below it (atr mode) — locks in profit
        # instead of riding a winner all the way back to the original
        # wide SL either way.
        cfg = config.load()
        if cfg.get("trail_sl_enabled", True):
            p["peak"] = max(p.get("peak", p["entry"]), ltp)
            trigger = p["entry"] * (1 + cfg.get("trail_sl_trigger_pct", 5) / 100)
            if p["peak"] >= trigger:
                if cfg.get("trail_sl_mode", "fixed_pct") == "atr":
                    regime = self.bus.get(f"regime:{sym}") or {}
                    atr_pct = regime.get("atr_pct")
                    if atr_pct:
                        gap_pct = atr_pct * cfg.get("atr_trail_multiplier", 1.5)
                        trail_to = round(p["peak"] * (1 - gap_pct / 100), 2)
                        mode_note = f"ATR-based, {atr_pct:.2f}% underlying ATR"
                    else:
                        # no ATR reading available yet (e.g. regime not
                        # computed this cycle) — fall back to fixed_pct
                        # rather than skip trailing entirely
                        trail_to = round(p["peak"] * (1 - cfg.get("trail_sl_gap_pct", 10) / 100), 2)
                        mode_note = "fixed_pct fallback (no ATR reading yet)"
                else:
                    trail_to = round(p["peak"] * (1 - cfg.get("trail_sl_gap_pct", 10) / 100), 2)
                    mode_note = "fixed_pct"
                if trail_to > p["stoploss"]:
                    p["stoploss"] = trail_to
                    self.bus.log(self.name,
                                 f"↗ {sym} trail SL → ₹{trail_to} "
                                 f"(peak ₹{p['peak']:.1f}, {mode_note})")
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            positions[sym] = p
            self.bus.set("positions", positions)
            self.bus.set("position", p)
        if reason:
            self.exit(reason, symbol=sym)
        else:
            self._option_ai_check(p, sym, ltp)
        return summary

    def _option_ai_check(self, p, sym, ltp):
        """2026-07-28 — per explicit request, mirrors _spread_ai_check
        exactly (same advisory-only-by-default design, same 5-minute
        cadence, same auto-exit opt-in pattern) but for single-leg
        option positions ("open trade"), which had no equivalent
        advisory at all until now. Also includes the market-move
        context (see _market_move_context) so the advisory factors in
        where price may go next, not just the position's own static
        entry/SL/target numbers."""
        cfg = config.load()
        if cfg.get("ai_engine", "local") == "off":
            return
        _risk = abs(p.get("entry", 0) - p.get("stoploss", 0)) * (p.get("qty") or 0)
        _near = False
        if p.get("stoploss") and p.get("entry"):
            _span = abs(p["entry"] - p["stoploss"]) or 1
            _near = abs(ltp - p["stoploss"]) / _span < 0.25
        _trig = ai_advisory_due(p, cfg, p.get("pnl", 0), _risk, _near)
        if not _trig:
            return
        p["ai_ts"] = time.time()
        p["ai_last_pnl"] = p.get("pnl", 0)
        _t0 = time.time()
        _do_exit = None   # decided in the try, EXECUTED after it (v59.71)
        try:
            import llm, json as _json
            market_ctx = self._market_move_context(sym)
            prompt = (
                "You monitor an open Indian index option BUY position "
                "(long call or put).\n"
                # 2026-08-06 — the prompt never stated the directional
                # relationship, so the model treated ANY trend as a
                # reason to exit. All five auto-exits ever taken cited a
                # trend that FAVOURED the position: every PE closed
                # because the market was "trending down", every CE
                # because it was "trending up". Say it explicitly, and
                # ask for the direction as a FIELD so the guard below
                # can check it structurally instead of regexing prose.
                "A CALL (CE) GAINS when the index RISES and loses when "
                "it falls. A PUT (PE) GAINS when the index FALLS and "
                "loses when it rises. A trend in the position's favour "
                "is a reason to HOLD, not to exit.\n"
                "Reply ONLY JSON: "
                "{\"advice\":\"HOLD|EXIT\",\"confidence\":0-100,"
                "\"market_dir\":\"UP|DOWN|FLAT\","
                "\"why\":\"<15 words\"}.\n"
                f"{sym} {p.get('strike')} {p.get('leg')}, entry "
                f"\u20b9{p.get('entry')}, current LTP \u20b9{ltp}, stop "
                f"\u20b9{p.get('stoploss')}, target1 \u20b9{p.get('target1')}, "
                f"target2 \u20b9{p.get('target2')}, current P&L "
                f"\u20b9{p.get('pnl', 0)}. "
                f"Market context (factor in where price may move next, "
                f"not just the position's own numbers): {market_ctx}.")
            text, engine, err = llm.generate_json(prompt, max_tokens=120)
            if err or not text:
                p["ai_advice"] = None if err == "ai_off" else f"AI unavailable ({err})"
                return
            j = _json.loads(text)
            if j and j.get("advice"):
                p["ai_advice"] = (f"{j['advice']} ({j.get('confidence', '?')}%)"
                                 f" — {j.get('why', '')} \u00b7 {engine}")
                confidence = int(j.get("confidence", 0))
                threshold = cfg.get("option_ai_exit_confidence_threshold", 75)
                _log_ai_advisory(self, sym, "option", j, confidence,
                                 threshold, cfg, p.get("pnl", 0),
                                 _trig, time.time() - _t0)
                if j["advice"] == "EXIT" and confidence >= threshold:
                    why = j.get("why", "")
                    self.bus.alert("medium", "execution", sym,
                                   f"AI suggests exiting {sym} {p.get('strike')} "
                                   f"{p.get('leg')}: {why}")
                    _contra = ai_exit_contradicts_position(
                        p.get("leg"), j.get("market_dir"))
                    _held = (time.time() - (p.get("opened_ts") or 0))
                    _min_hold = cfg.get("option_ai_min_hold_sec", 120)
                    if _contra:
                        # See ai_exit_contradicts_position(): 5 of 5
                        # auto-exits ever taken were of this shape.
                        self.bus.log(self.name,
                                     f"AI auto-exit BLOCKED for {sym} "
                                     f"{p.get('strike')} {p.get('leg')} — the "
                                     f"stated move ({j.get('market_dir')}) "
                                     f"FAVOURS this position: {why}")
                    elif p.get("opened_ts") and _held < _min_hold:
                        # "shows no profit" one second after entry is
                        # vacuous — no position shows profit one second
                        # in. FINNIFTY 26900 CE, 15:01:33 -> 15:01:34.
                        self.bus.log(self.name,
                                     f"AI auto-exit DEFERRED for {sym} "
                                     f"{p.get('strike')} {p.get('leg')} — held "
                                     f"{_held:.0f}s < {_min_hold}s minimum: {why}")
                    elif cfg.get("option_ai_auto_exit_enabled", False):
                        self.bus.log(self.name,
                                     f"AI auto-exit ENABLED — closing {sym} "
                                     f"{p.get('strike')} {p.get('leg')} on AI "
                                     f"advisory ({confidence}%): {why}")
                        _do_exit = f"AI advisory EXIT ({confidence}%): {why}"
        except Exception as e:
            p["ai_advice"] = f"AI check unavailable ({e})"
        # v59.71 (third-eye Tier 4) — exit executes OUTSIDE the advice
        # try, so an exit() bug can no longer be relabelled "AI check
        # unavailable" while the position stays open.
        if _do_exit:
            self.exit(_do_exit, symbol=sym)

    def exit(self, reason="manual exit", symbol=None):
        positions = self.bus.get("positions", {}) or {}
        if symbol is None:
            # backward-compat: no symbol given -> exit the single/most
            # recent position (used by the manual "Exit position" button
            # when only one trade is open)
            symbol = next(iter(positions), None)
        p = positions.get(symbol)
        if not p:
            return {"error": "no open position"}
        if p["paper"]:
            self.bus.log(self.name, f"📄 PAPER SELL {p['qty']} x {p['symbol']} "
                         f"{p['strike']} {p['leg']} @ ₹{p['ltp']} — {reason} "
                         f"· P&L ₹{p['pnl']:.0f}")
        else:
            # v59.69 (third-eye Tier 3) — retry cooldown. A timed-out
            # SELL may have reached the exchange; the old flow returned
            # the error, left the position in the book, and _monitor_one
            # re-hit the same exit reason 2 seconds later — firing a
            # SECOND market SELL with the first one possibly filled (the
            # kill-switch loop would amplify this into an order storm).
            # There is no order_status() polling yet, so the safe floor
            # is: after a failed/unknown placement, refuse to re-place
            # for exit_retry_cooldown_sec and demand a manual check.
            _cool = int(config.load().get("exit_retry_cooldown_sec", 30))
            _last = p.get("exit_attempt_ts") or 0
            if _last and time.time() - _last < _cool:
                return {"error": f"exit retry cooling down "
                                 f"({time.time() - _last:.0f}s of {_cool}s) — "
                                 f"previous SELL status unknown, verify at "
                                 f"the broker"}
            orders = self.ctx["orders_factory"]()
            if orders and p.get("security_id"):
                p["exit_attempt_ts"] = time.time()
                positions[symbol] = p
                self.bus.set("positions", positions)
                try:
                    resp = orders.place(p["symbol"], p["security_id"], "SELL",
                                        p["qty"], "MARKET")
                    self.bus.log(self.name, f"🔴 LIVE SELL — order "
                                 f"{resp.get('orderId','UNCONFIRMED')} — {reason} "
                                 f"· est P&L ₹{p['pnl']:.0f}")
                    _st = self._confirm_order(orders, resp,
                                              f"SELL {p['symbol']} "
                                              f"{p.get('strike')} {p.get('leg')}")
                    # v59.72 (R2 finding H2) — a REJECTED exit must NOT
                    # book a close: the position is still live at the
                    # broker. Keep it; exit_attempt_ts (stamped above)
                    # paces the retries.
                    if _st in ("REJECTED", "CANCELLED"):
                        return {"error": f"exit order {_st} at the broker "
                                         f"— position kept; retry after "
                                         f"cooldown"}
                    # v59.75 — the exit P&L is computed from the REAL
                    # fill when the trade book answers, not from the
                    # last monitored premium.
                    _fpx, _ = self._actual_fill(orders, resp, p["qty"],
                                                f"SELL {p['symbol']}")
                    if _fpx:
                        p["exit_fill_slippage"] = round(
                            _fpx - (p.get("ltp") or _fpx), 2)
                        p["ltp"] = _fpx
                        p["pnl"] = round((_fpx - p["entry"]) * p["qty"], 0)
                except Exception as e:
                    # An ALERT, not a log line: "close manually NOW" is
                    # the single most urgent operational event this
                    # system can produce, and it used to go to a feed
                    # deque that rotates out in ~13 minutes.
                    self.bus.alert("high", self.name, p["symbol"],
                                   f"LIVE EXIT FAILED ({type(e).__name__}: {e}) "
                                   f"— close {p['symbol']} {p.get('strike')} "
                                   f"{p.get('leg')} manually on the broker NOW; "
                                   f"auto-retry in {_cool}s")
                    return {"error": str(e)}
            else:
                self.bus.log(self.name, "⚠ no broker for live exit — close "
                                        "manually on Dhan")
        now = now_ist()
        # fees: ₹fee_per_lot per lot per transaction; entry + exit = 2
        cfg = config.load()
        lots = p.get("lots") or max(1, round(p["qty"] / cfg["lot_sizes"].get(p["symbol"], 75)))
        _c = realistic_costs("option", p.get("symbol"), lots,
                             p.get("entry"), p.get("ltp") or p.get("entry"),
                             cfg, legs=1,
                             log=lambda m: self.bus.log(self.name, m))
        fees, slippage = _c["fees"], _c["slippage"]
        # v59.68 — this was the one exit path WITHOUT the zero-fee tripwire
        # (futures and spreads both had it), and it is the highest-volume one.
        warn_zero_fees(self.bus, self.name, "option", lots, fees)
        gross = p.get("pnl", 0)
        closed = dict(p,
                      closed=now.strftime("%H:%M:%S"),
                      closed_date=now.strftime("%Y-%m-%d"),
                      closed_at=now.isoformat(),
                      gross_pnl=gross,
                      fees=fees,
                      slippage=slippage,
                      cost_model=_c.get("model"),   # v59.68 — fallback visible in the record
                      pnl=round(gross - fees - slippage, 0),   # NET of BOTH cost parts
                      reason=reason)
        self.bus.set("position", None)
        positions.pop(symbol, None)
        self.bus.set("positions", positions)
        if positions:
            # legacy mirror points at whatever's still open (dashboard
            # code that reads a single "position" still gets something
            # useful rather than None while other trades remain open)
            self.bus.set("position", next(iter(positions.values())))
        _record_closed(self.bus, closed)   # capped window (v59.71)
        _append_trade(closed)          # persist to disk immediately
        # RE-ENTRY COOLDOWN — stamp symbol+strike+leg on the way out.
        # 2026-08-06: spreads, futures, broker failures and news
        # re-alerts all had cooldowns; directional options had none, so
        # a still-valid signal re-entered the instant its predecessor
        # closed. SENSEX 78700 CE went in and out five times in 38s.
        # This is the BACKSTOP of the three fixes — the target2 repair
        # and the live-price fill remove the causes; this one caps the
        # damage of whatever causes an instant exit next.
        _cd = self.bus.get("option_reentry_block") or {}
        _cd[f"{p['symbol']}:{p['strike']}:{p['leg']}"] = time.time()
        # v59.71 — prune lapsed entries while writing: every strike ever
        # traded used to keep its key for the process lifetime. Kept for
        # 2x the cooldown so a raised cooldown still honours older stamps.
        _ttl = 2 * max(60, int(cfg.get("option_reentry_cooldown_sec", 180) or 180))
        _now_prune = time.time()
        _cd = {k: v for k, v in _cd.items() if _now_prune - v < _ttl}
        self.bus.set("option_reentry_block", _cd)
        self.bus.alert("high", "execution", p["symbol"],
                       f"Exited {p['strike']} {p['leg']} — {reason} — "
                       f"P&L ₹{p['pnl']:.0f}")
        self.bus.publish("closed", closed)
        # Series Markers, per explicit request — classify the exit
        # kind from the SAME `reason` string _monitor_one already
        # builds ("stoploss (...)"/"target-2 (...)"/etc.), rather than
        # re-deriving a separate classification. Spot price reused from
        # the same chain fetch _monitor_one already made this cycle
        # (bus key, not a new API call).
        reason_lower = reason.lower()
        if "stoploss" in reason_lower or "stop loss" in reason_lower:
            kind = "stop_hit"
        elif "target" in reason_lower:
            kind = "target_hit"
        else:
            kind = "exit"
        chain = self.bus.get(f"chain:{symbol}")
        exit_spot = chain.get("spot") if chain else None
        self._record_chart_event(symbol, kind, exit_spot,
                                 f"{reason} · P&L ₹{p['pnl']:.0f}")
        return {"closed": closed}


class LearningAgent(Agent):
    name = "learning"
    interval = 300

    def cycle(self):
        t = now_ist()
        today = t.strftime("%Y-%m-%d")
        # Chain-snapshot retention (2026-07-26, v53) — history.
        # prune_chain_snapshots() has existed since the Institutional
        # Activity Engine build but was never wired to anything that
        # actually calls it, so chain_snapshots has been growing
        # unbounded ever since (roadmap estimate: ~240k rows/day across
        # 4 symbols at the 60s snapshot cadence). Deliberately NOT tied
        # to the journal's 15:35/done-today gate below — pruning old
        # rows has nothing to do with whether today's trades have
        # closed yet, so it gets its OWN once-per-day bus key and runs
        # from the top of cycle(), independent of the journal's early
        # returns. LearningAgent already runs on a 300s cadence and is
        # the closest thing this codebase has to a daily-maintenance
        # agent, so this rides along rather than spinning up a new one.
        # daily_marks, not the bus (2026-08-08). The Bus is in-memory,
        # so this "once per day" gate was really once per RESTART:
        # nine prune runs between 00:01 and 00:20 while v59.53..58
        # were deployed, each scanning a 752k-row table, each
        # holding the write lock past other writers' 30s
        # busy_timeout ("futures OI archive FAILED ... database is
        # locked"), and each thinning NOTHING. The bus key is still
        # set so anything introspecting the blackboard sees it.
        if not daily_marks.done("chain_prune_done", today):
            try:
                import history
                # v59.0 item 18 — this passed a 5-day retention and hard-
                # deleted everything older. That is why the September 2025
                # question and item 16 (repricing the replays from real
                # premiums) are both unanswerable today: the premiums were
                # thrown away nightly. Now tiered — full for 90 days, 5-min
                # thereafter, daily close past 2 years. Argument-free call
                # so retention lives in one place, in history.py.
                res = history.prune_chain_snapshots()
                # v58.32 - same daily maintenance slot; see
                # history.prune_ta_calibration() for the sizing note.
                history.prune_ta_calibration(config.load().get('ta_calibration_retention_days', 10))
                daily_marks.mark("chain_prune_done", today)
                self.bus.set("chain_prune_done", today)
                self.bus.log(self.name, f"chain_snapshots retention: {res}")
                # v59.0 item 32 — contract sizes drift silently. ~24 call
                # sites read cfg["lot_sizes"]; only futures_costs asks the
                # scrip master. Surface the divergence daily rather than
                # letting someone find it while building an unrelated panel.
                import futures_costs as _fc
                # 2026-08-02 — same idea, different numbers: the per-trade
                # budget, the two per-trade caps and the portfolio cap are
                # set independently and silently disagree. Surface it.
                import sizing as _szc
                for _rc in _szc.risk_coherence():
                    self.bus.log(self.name, f"\u26a0 RISK CONFIG: {_rc}")
                for _mm in _fc.reconcile_lot_sizes():
                    self.bus.log(self.name,
                                 f"\u26a0 LOT SIZE DRIFT {_mm['symbol']}: config "
                                 f"{_mm.get('config')} vs scrip master "
                                 f"{_mm.get('scrip')} ({_mm.get('pct')}%) — every "
                                 f"notional, margin and P&L figure for this "
                                 f"symbol is wrong by that factor")
            except Exception as e:
                # Fail loud, not silent — same convention as every other
                # maintenance task in this codebase. A failed prune
                # should be visible, not a permanently-growing table
                # nobody finds out about until disk fills up.
                self.bus.log(self.name, f"\u26a0 chain_snapshots prune "
                             f"FAILED: {type(e).__name__}: {e}")
        # Long-window ATM IV series (2026-08-08). risk_engine.
        # backfill_iv_history() was written, correct, and called by
        # NOTHING \u2014 the same failure prune_chain_snapshots() had above,
        # found the same way. `daily_atm_iv` was empty on every symbol,
        # so agents.py's own IV-percentile tier (which READS that table
        # via history.get_daily_atm_iv_history) has silently had no
        # long-window source since it was built, and the strategy-reset
        # memo could not evaluate the volatility risk premium at all.
        #
        # Its OWN bus key, not chain_prune_done: the prune block above
        # sets its key mid-try, so sharing it would let a prune failure
        # skip the backfill forever \u2014 and these two have nothing to do
        # with each other beyond running once a day.
        #
        # Cheap in steady state: the function skips any day already in
        # daily_atm_iv, so a normal run reconstructs ONE new day per
        # symbol. Only the first run after this ships does real work.
        # Same persistence as the prune above — this key had the
        # identical flaw, introduced in v59.53 by following the
        # existing convention without questioning it.
        if not daily_marks.done("iv_backfill_done", today):
            try:
                import risk_engine as _re
                # bus "symbols", the same list every other agent walks —
                # config.py's own comment calls it the list that "drives
                # strategy, risk and ...". There is no module-level
                # SYMBOLS constant to fall back to.
                for _sym in self.bus.get("symbols", []):
                    _r = _re.backfill_iv_history(_sym)
                    if _r.get("days_processed"):
                        self.bus.log(self.name,
                                     f"ATM IV backfill {_sym}: {_r}")
                daily_marks.mark("iv_backfill_done", today)
                self.bus.set("iv_backfill_done", today)
            except Exception as e:
                # Same convention as the prune above. An IV series that
                # silently stops growing is exactly how this table came
                # to be empty in the first place.
                self.bus.log(self.name, f"\u26a0 ATM IV backfill FAILED: "
                             f"{type(e).__name__}: {e}")
        done = self.bus.get("journal_done")
        all_trades = self.bus.get("closed_trades", [])
        # Bug found 2026-07-26: `closed_trades` is loaded at startup
        # from the FULL persisted history (`load_persisted_trades()` —
        # "restored N historical trades") and new exits are simply
        # appended to that same list all session — it is NOT reset
        # daily. This "daily" journal was computing pnl/wins/trade-
        # count over the entire lifetime history every single day, not
        # today's trades, because nothing here ever filtered by date.
        # Fixed: use each trade's own `closed_date` field (already
        # recorded by exit(), just never used here).
        trades = [x for x in all_trades if x.get("closed_date") == today]
        if done == today:
            self.summary = "today's journal written"
            return
        # 2026-08-03 — was 15:35, which is now BEFORE the 15:40 F&O close;
        # trades closing in that window would land after the day was
        # written. Same attribution failure as the double-counted days.
        if t.hour * 60 + t.minute < _session_min("fno_close_time", "15:40") + 5:
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
        # 2026-07-27 (item 6) — retroactive candle-by-candle audit, per
        # explicit repeated request ("Apply Loop - Analysis, learn and
        # adopt", "backtest for each candle and each strategy to
        # identify the gap"). Runs automatically once per day, for
        # every (symbol, strategy) pairing that actually traded today
        # — reuses backtester.audit_today() (which itself reuses the
        # v56 optimizer's existing _replay_for()/get_params()/metrics(),
        # no new strategy logic) to compare what the pure rules would
        # have done today against what actually happened, surfacing
        # genuine gaps rather than a single aggregate number. Wrapped
        # per-pairing so one failing audit can't block the journal
        # write or the other pairings' audits.
        audits = {}
        try:
            import backtester
            pairings = {(t.get("symbol"), t.get("strategy") or t.get("source"))
                       for t in trades if t.get("symbol") and
                       (t.get("strategy") or t.get("source"))}
            for sym, name in pairings:
                try:
                    full = backtester.audit_today(
                        name, sym, all_trades, log=lambda m: self.bus.log(self.name, m))
                    # Trimmed for persistence — journal.json already
                    # accumulates indefinitely (2.5MB+ observed live);
                    # the full backtest_trades/matched detail is large
                    # and available on demand via the API instead of
                    # being written to disk every single day forever.
                    audits[f"{sym}:{name}"] = {
                        "gap_summary": full["gap_summary"],
                        "backtest_net_pnl": full["backtest_metrics"].get("net_pnl"),
                        "real_net_pnl": full["real_net_pnl"],
                        "backtest_trade_count": len(full["backtest_trades"]),
                        "matched_count": len(full["matched"]),
                        "missed_by_live_count": len(full["missed_by_live"]),
                        "unexpected_in_live_count": len(full["unexpected_in_live"])}
                except Exception as e:
                    self.bus.log(self.name, f"\u26a0 audit failed for {sym}/{name}: "
                                f"{type(e).__name__}: {e}")
        except Exception as e:
            self.bus.log(self.name, f"\u26a0 daily audit step failed entirely: "
                        f"{type(e).__name__}: {e}")
        entry = {**stats, "critique": critique, "trades_detail": trades,
                "daily_audit": audits}
        journal = []
        if os.path.exists(JOURNAL):
            try:
                journal = json.load(open(JOURNAL))
            except Exception:
                pass
        # 2026-07-28 — real bug found from live data: journal_done is an
        # IN-MEMORY bus flag, which does not survive a server restart —
        # but journal.json itself DOES persist. Every restart that
        # happened after 15:35 IST on a given day re-ran this whole
        # block (since the fresh process's `done` was None again) and
        # blindly appended ANOTHER entry for the SAME date, rather than
        # recognizing one already existed. Confirmed directly: some
        # dates had up to 8 duplicate entries, all with identical
        # numbers, from repeated restarts during active development.
        # Fixed at the actual source of truth (the file itself, not the
        # in-memory flag alone): look for an existing entry with this
        # exact date and REPLACE it in place — a later run in the same
        # day could genuinely have more complete data (e.g. a trade
        # that closed after an earlier run already wrote) — rather than
        # appending a duplicate every time.
        journal = [j for j in journal if j.get("date") != today]
        journal.append(entry)
        json.dump(journal, open(JOURNAL, "w"), indent=2, default=str)
        self.bus.set("journal_done", today)
        self.bus.set("journal_latest", entry)
        # 2026-07-27 (item 12) — ML probability scoring: checks the
        # ACTUAL current Shadow Journal volume once per day (same
        # once-daily cadence as the journal itself) and trains a real
        # model the moment volume becomes sufficient, rather than this
        # staying a standing "not enough yet" assumption forever.
        # Persisted to the bus so /api/ml-probability/status and any
        # future consumer can read the latest trained model without
        # re-scanning the shadow journal file on every request.
        try:
            import ml_probability as ml
            ml_result = ml.train_model(closed_trades=all_trades)
            self.bus.set("ml_probability_status", ml_result["volume"])
            if ml_result["trained"]:
                self.bus.set("ml_probability_model", ml_result["model"])
                self.bus.log(self.name, f"ML probability model trained — "
                            f"{ml_result['volume']['reason']}")
            else:
                self.bus.log(self.name, f"ML probability: still waiting on "
                            f"volume — {ml_result['volume']['reason']}")
        except Exception as e:
            self.bus.log(self.name, f"\u26a0 ML probability check failed: "
                        f"{type(e).__name__}: {e}")
        # AI Learning Engine feedback loop (Feature #8) — per explicit
        # request to make the journal actually feed BACK into signal
        # generation, not just report on yesterday. Computes named,
        # LOGGED pattern-performance flags from the FULL persisted
        # history (all_trades, not just today — a pattern needs more
        # than one day's trades to say anything reliable) and stores
        # them for RiskAgent to check on every future signal. Reuses
        # ai_probability_engine's own bucketing convention (confidence
        # band × institutional agreement × regime) for consistency —
        # this is the SAME empirical grouping, just aggregated into
        # named patterns with a minimum sample size before flagging
        # anything, rather than looked up per-signal.
        try:
            import ai_probability_engine as ape
            patterns = {}
            for trade in all_trades:
                if trade.get("entry_confidence") is None:
                    continue
                key = (ape._bucket_confidence(trade["entry_confidence"]),
                      trade.get("entry_institutional_agreement"),
                      trade.get("entry_regime"))
                p = patterns.setdefault(key, {"wins": 0, "total": 0})
                p["total"] += 1
                if trade.get("pnl", 0) > 0:
                    p["wins"] += 1
            underperforming = []
            min_sample = 5
            # v59.66 (third-eye Tier 1) — a raw win_rate < 35% on n=5 was
            # a 5-sample decision rule suppressing whole signal classes:
            # 1/5 wins is consistent with a 43%+ true rate. Flag only
            # when the 95% Wilson UPPER bound sits below the threshold —
            # i.e. when the data actually rules 35% out, not merely
            # fails to reach it. At 0 wins that needs n≥10; at real
            # mixtures, proportionally more.
            from promotion_gate import wilson_upper
            for (bucket, inst_agree, regime), p in patterns.items():
                if p["total"] < min_sample:
                    continue
                win_rate = p["wins"] / p["total"]
                if wilson_upper(p["wins"], p["total"]) < 0.35:
                    underperforming.append({
                        "confidence_bucket": bucket, "institutional_agreement": inst_agree,
                        "regime": regime, "win_rate": round(win_rate * 100),
                        "wilson_upper_pct": round(
                            wilson_upper(p["wins"], p["total"]) * 100),
                        "sample_size": p["total"]})
            self.bus.set("learned_underperforming_patterns", underperforming)
            if underperforming:
                self.bus.log(self.name, f"⚠ {len(underperforming)} underperforming "
                                        f"pattern(s) flagged for tomorrow's risk gate — "
                                        + "; ".join(
                                            f"conf {u['confidence_bucket']}/regime "
                                            f"{u['regime']}: {u['win_rate']}% win rate "
                                            f"(n={u['sample_size']})" for u in underperforming))
        except Exception as e:
            self.bus.log(self.name, f"⚠ pattern-performance analysis failed: {e}")
        self.summary = f"journal: {len(trades)} trades, P&L ₹{pnl:.0f}"
        self.bus.log(self.name, self.summary)
        self._weekly_risk_analytics(t)

    def _weekly_risk_analytics(self, t):
        """Weekly Risk Analytics (Feature #9) — per the spec's own
        explicit instruction: "Generate weekly risk analytics.
        Recommend parameter tuning. Never automatically modify
        production limits." Runs once per ISO week (tracked via a
        bus flag, same pattern as the daily journal's `journal_done`),
        reusing `history.get_risk_decisions()` (this session's own new
        DB table) and `closed_trades` — no new data collection, purely
        analysis of what's already being persisted every risk decision.

        Deliberately generates RECOMMENDATIONS only, written to a bus
        key and the journal file for a human to read and manually
        apply via Settings if they agree — nothing here ever calls
        config.save() or otherwise touches production limits, matching
        the spec's explicit prohibition directly."""
        week_id = t.strftime("%G-W%V")   # ISO week identifier
        if self.bus.get("weekly_risk_done") == week_id:
            return
        # Only run once the week has genuinely had a few trading days —
        # avoid generating a "weekly" report off a single day's data
        # right after a fresh restart.
        week_start_date = (t - timedelta(days=t.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        # Bug found in testing: the first draft computed `t -
        # timedelta(days=t.weekday())` without zeroing the time-of-day
        # — for a Monday AFTERNOON that gives "today at the current
        # time", not "midnight Monday", silently excluding everything
        # from earlier that same day (and, since weekday()=0 means
        # zero days subtracted, effectively the whole week's earlier
        # data too). Fixed with .replace() to zero the clock.
        try:
            import history
            decisions = history.get_risk_decisions(
                since_ts=week_start_date.timestamp(), limit=2000)
        except Exception as e:
            self.bus.log(self.name, f"⚠ weekly risk analytics failed to read decisions: {e}")
            return
        if len(decisions) < 10:
            return   # not enough data yet to say anything meaningful

        approved = [d for d in decisions if d["verdict"] == "APPROVED"]
        rejected = [d for d in decisions if d["verdict"] == "REJECTED"]
        by_risk_level = {}
        for d in decisions:
            lvl = d.get("risk_level") or "Unknown"
            by_risk_level.setdefault(lvl, {"approved": 0, "rejected": 0})
            by_risk_level[lvl]["approved" if d["verdict"] == "APPROVED" else "rejected"] += 1

        trades = self.bus.get("closed_trades", [])
        week_trades = [x for x in trades if x.get("closed_date", "") >= week_start_date.strftime("%Y-%m-%d")]
        win_rate = (round(sum(1 for x in week_trades if x.get("pnl", 0) > 0) /
                         len(week_trades) * 100) if week_trades else None)

        summary = {"week": week_id, "total_decisions": len(decisions),
                  "approved": len(approved), "rejected": len(rejected),
                  "by_risk_level": by_risk_level, "trades_closed": len(week_trades),
                  "win_rate_pct": win_rate}

        cfg = config.load()
        recommendation = ""
        if cfg.get("ai_engine", "local") != "off":
            try:
                recommendation = claude(
                    "You are a risk management consultant reviewing a week of "
                    "an automated options-trading system's risk decisions. Given "
                    "this summary (approval/rejection counts by risk level, "
                    "trades closed, win rate), in at most 5 bullet points: is "
                    "the current risk gate too loose, too tight, or reasonably "
                    "calibrated, and what ONE specific parameter (e.g. min_"
                    "confidence, daily_loss_limit, max_concurrent_positions) "
                    "would you suggest reviewing, with a brief reason. Be "
                    "specific and concise. Do NOT claim to have changed "
                    "anything — you are only recommending.\n\n" +
                    json.dumps(summary, default=str), None, 400)
            except Exception as e:
                recommendation = f"(AI review unavailable: {e})"

        entry = {**summary, "recommendation": recommendation,
                "generated_at": t.isoformat()}
        weekly_journal = []
        if os.path.exists(WEEKLY_RISK_JOURNAL):
            try:
                weekly_journal = json.load(open(WEEKLY_RISK_JOURNAL))
            except Exception:
                pass
        # 2026-07-28 — same restart-vulnerability bug as the daily
        # journal above (weekly_risk_done is in-memory only, doesn't
        # survive a restart, journal file does) — confirmed directly:
        # the SAME week_id appeared 4 times in a live file, each
        # generated at a different timestamp after separate restarts.
        # Same fix: replace any existing entry for this week_id rather
        # than blindly appending another one.
        weekly_journal = [w for w in weekly_journal if w.get("week") != week_id]
        weekly_journal.append(entry)
        json.dump(weekly_journal, open(WEEKLY_RISK_JOURNAL, "w"), indent=2, default=str)
        self.bus.set("weekly_risk_done", week_id)
        self.bus.set("weekly_risk_latest", entry)
        self.bus.log(self.name, f"📊 weekly risk analytics generated ({week_id}): "
                                f"{len(approved)} approved / {len(rejected)} rejected, "
                                f"win rate {win_rate}% — recommendation logged, "
                                f"NOT auto-applied")


# ================================================================== orchestrator



class BacktestAgent(Agent):
    """Daily historical archive + strategy validation (post 15:45 IST).

    Cycle: after close -> archive today's chains + index candles, replay
    every strategy over the full local archive, store metrics, then
    revalidate live results vs backtest (retune -> re-test -> deploy or
    roll back, with reasons logged per requirement 8-11)."""
    name, interval = "backtest", 20

    RESULTS = store.path("backtests.json")

    def cycle(self):
        import backtester, history
        now = now_ist()
        ran = self.bus.get("bt_last_run")
        today = now.strftime("%Y-%m-%d")
        job = self.bus.get("bt_manual_job")
        if job:
            self.bus.set("bt_manual_job", None)
            self._run(job.get("sync", False))
            return
        # v56 — on-demand parameter sweep, queued the same way a manual
        # backtest run already is (via a bus job checked at the top of
        # this cycle) rather than run inline on the request thread —
        # a coordinate-wise sweep is several full backtest replays, real
        # seconds to tens of seconds, too slow for a synchronous HTTP
        # response.
        opt_job = self.bus.get("bt_optimize_job")
        if opt_job:
            self.bus.set("bt_optimize_job", None)
            self._optimize(opt_job["name"], opt_job["symbol"])
            return
        if now.hour * 60 + now.minute < 15 * 60 + 45 or ran == today:
            last = self.bus.get("bt_last_summary")
            self.summary = ("idle · " + last) if last else \
                "idle — daily run at 15:45; use Run backtest for manual run"
            return
        self.bus.set("bt_last_run", today)
        self._run(sync=True)

    def _optimize(self, name, symbol):
        """v56 — runs backtester.sweep_params() and, if it finds a
        meaningfully better parameter set, appends it as a new pending
        version using the IDENTICAL version-record shape _tune_pa/
        _revalidate already produce (same v/params/reason/created/
        results/deployed fields) — so the existing version-history
        modal, rollback, and activation UI all work on a sweep-proposed
        version with zero new UI needed."""
        import backtester
        self.summary = f"optimizing {symbol} {name}..."
        self.bus.log(self.name, f"optimize: sweeping {symbol} {name} "
                     f"for a better parameter value (not just re-testing "
                     f"the current one)")
        try:
            result = backtester.sweep_params(
                name, symbol, log=lambda m: self.bus.log(self.name, m))
        except Exception as e:
            self.bus.log(self.name, f"optimize {symbol} {name} FAILED: "
                         f"{type(e).__name__}: {e}")
            self.summary = f"optimize failed: {e}"
            return
        vers = backtester.load_versions()
        entry = backtester._symbol_entry(vers, name, symbol)
        cfg = config.load()
        min_conf = cfg.get("pa_min_trades_for_confidence", 15)
        self.bus.set(f"bt_last_sweep:{symbol}:{name}", {
            "at": backtester._now(), "tried": result["tried"],
            "improved": result["improved"],
            "baseline_net": result["baseline_metrics"].get("net_pnl"),
            "best_net": result["best_metrics"].get("net_pnl")})
        # v59.67 — `improved` (meaningful_improvement) also fires on a
        # 15%-smaller LOSS, and this path then appended a negative
        # version on every Optimize click. A sweep result becomes a
        # version only when it is positive AND meaningfully better;
        # everything tried is in trial_log either way.
        _base_pnl = (result["baseline_metrics"].get("net_pnl") or 0
                     if result["baseline_metrics"].get("trades") else 0)
        _best_pnl = (result["best_metrics"].get("net_pnl") or 0
                     if result["best_metrics"].get("trades") else 0)
        if not backtester.version_worthy(
                _base_pnl, _best_pnl,
                cfg.get("pa_tuning_improvement_threshold", 0.15)):
            self.summary = (f"{symbol} {name}: swept {len(result['tried'])} "
                           f"candidates — best (₹{_best_pnl:.0f}) is not a "
                           f"positive improvement over ₹{_base_pnl:.0f}; no "
                           f"version created (trial_log keeps the record)")
            self.bus.log(self.name, self.summary)
            return
        # v56 — guard against piling up duplicate versions: if the
        # active version isn't profitable enough to auto-activate (the
        # common case for a fresh sweep), get_params() keeps returning
        # the SAME baseline on every re-run, so clicking Optimize twice
        # would otherwise propose the identical params/result as a new
        # version each time. Skip if the most recent version already
        # has these exact params.
        latest = max(entry["versions"], key=lambda v: v["v"])
        if latest["params"] == result["best_params"]:
            self.summary = (f"{symbol} {name}: swept {len(result['tried'])} "
                           f"candidates — v{latest['v']} already IS the "
                           f"best found, nothing new to propose")
            self.bus.log(self.name, self.summary)
            return
        newv = max(x["v"] for x in entry["versions"]) + 1
        new_trades = result["best_metrics"].get("trades") or 0
        new_pnl = result["best_metrics"].get("net_pnl") or 0
        # v59.66 — "profitable" here decides only whether the sweep's
        # winner becomes the ACTIVE paper version. Live is the gate's
        # call, and a swept result is the definition of in-sample.
        new_profitable = new_trades >= min_conf and new_pnl > 0
        entry["versions"].append({
            "v": newv, "params": result["best_params"],
            "reason": (f"on-demand optimizer sweep — searched "
                      f"{len(result['tried'])} candidate values across "
                      f"this strategy's tunable parameters (not just the "
                      f"single daily auto-tune nudge) and found a better "
                      f"one than the active v{entry['active']}"),
            "created": backtester._now(), "last_tested": backtester._now(),
            "results": result["best_metrics"], "deployed": new_profitable})
        if new_profitable:
            entry["active"] = newv
            # v59.66 — a swept winner is in-sample by definition; the
            # gate scores only its (still empty) out-of-sample window,
            # so live_enabled starts False and is re-scored daily.
            import promotion_gate
            try:
                _gok, _ = promotion_gate.evaluate_entry(
                    name, symbol, result["best_metrics"])
            except Exception:
                _gok = False
            entry["live_enabled"] = bool(_gok)
        backtester.save_versions(vers)
        self.summary = (f"{symbol} {name}: optimizer found v{newv} "
                       f"(₹{new_pnl:.0f}/{new_trades}t) — "
                       f"{'activated for paper' if new_profitable else 'proposed, review to activate'}")
        self.bus.alert("medium", self.name, f"{symbol}:{name}",
                       f"Optimizer proposed {symbol} {name} v{newv} "
                       f"(₹{new_pnl:.0f}, {len(result['tried'])} candidates "
                       f"searched) — "
                       f"{'activated automatically' if new_profitable else 'review in Backtest page to activate'}")

    def _run(self, sync):
        import backtester, history
        dhan = self.ctx["dhan_client"]()
        syms = self.bus.get("symbols", ["NIFTY"])
        if sync and dhan:
            for sym in syms:
                try:
                    def prog(m, _s=sym):
                        self.summary = f"archiving {_s}: {m}"
                    # Bug found 2026-07-22: this was `log=lambda m: None`
                    # — a no-op. sync_day_chain() has detailed diagnostic
                    # logging for exactly this kind of failure ("no
                    # candles today" vs "chain sync FAILED after
                    # retries" vs "N legs failed"), but the automated
                    # daily run threw all of it away, which is exactly
                    # why SENSEX repeatedly showing 0 chain days had no
                    # visible cause anywhere in the logs.
                    history.sync_day_chain(self.ctx["get_chain"], dhan, sym,
                                           log=lambda m: self.bus.log(self.name, m),
                                           progress=prog)
                    # B9 (2026-08-04). The index freezes at 15:15 under
                    # the new CAS rules and futures trade on to 15:40, so
                    # futures are the only instrument with real prices in
                    # that window — and we archived their OI but never
                    # their CANDLES. Same day, same driver, same pacing as
                    # the option legs above.
                    history.sync_futures_candles(
                        dhan, sym, now_ist().strftime("%Y-%m-%d"),
                        log=lambda m: self.bus.log(self.name, m))
                except Exception as e:
                    self.bus.log(self.name, f"sync {sym}: {str(e)[:200]}")
            # ---- Phase 1 watchlist: ARCHIVE ONLY, never traded.
            # 2026-08-04. These names are not in the bus "symbols" list —
            # that list drives strategy, risk and execution, so a name
            # there would be traded. This loop exists so a candidate
            # instrument accumulates real chain and futures history, and
            # its liquidity can be measured, BEFORE anyone decides
            # whether it is worth trading. The order is deliberate: the
            # promotion gate already refuses a strategy with no `own_sd`,
            # and an instrument deserves the same treatment.
            #
            # Chains are fetched straight from the broker rather than
            # through ctx["get_chain"], which carries index-shaped bus
            # caching this path has no business touching.
            # `cfg` is NOT in scope in _run() — referencing it raised
            # NameError on every daily cycle from v59.22 until
            # 2026-08-05, so this loop never ran once and ADANIENSOL
            # archived nothing. It failed LOUDLY ("name 'cfg' is not
            # defined") and was still missed, because the message names
            # no context and the test asserted the STRING was present
            # rather than executing the loop.
            for wsym in (config.load().get("watch_symbols") or []):
                try:
                    import instrument_registry as _ireg
                    ok, why, _d = _ireg.validate(wsym)
                    if not ok:
                        self.bus.log(self.name, f"⚠ watch symbol {wsym}: {why}")
                        continue
                    history.sync_day_chain(lambda s: dhan.option_chain(s),
                                           dhan, wsym,
                                           log=lambda m: self.bus.log(self.name, m),
                                           progress=lambda m, _s=wsym: None)
                    history.sync_futures_candles(
                        dhan, wsym, now_ist().strftime("%Y-%m-%d"),
                        log=lambda m: self.bus.log(self.name, m))
                except Exception as e:
                    self.bus.log(self.name,
                                 f"watch-symbol archive {wsym}: {str(e)[:180]}")
        self.bus.set("bt_coverage", history.coverage())
        results = {}
        for sym in syms:
            if not history.chain_days(sym) and not history.index_days(sym, 1):
                continue
            self.summary = f"backtesting {sym}..."
            try:
                _fee_warn = backtester.warn_if_costs_disabled()
                if _fee_warn:
                    self.bus.log(self.name, _fee_warn)
                    self.bus.alert("high", self.name, sym, "Backtest costs disabled")
                # v59.74 — errors inside run_all used to vanish into a
                # lambda that discarded them; the feed gets them now.
                results[sym] = backtester.run_all(
                    sym, log=lambda m: self.bus.log(self.name, m))
            except Exception as e:
                self.bus.log(self.name, f"backtest {sym}: {str(e)[:70]}")
        json.dump({"at": now_ist().isoformat(), "results": results},
                  open(self.RESULTS, "w"), indent=1)
        self.bus.set("bt_results", results)
        self._revalidate(results)
        self._tune_pa(results)
        self.summary = "backtest complete: " + ", ".join(results) if results             else "no archived chain days yet — archive builds daily from close"
        self.bus.log(self.name, self.summary)

    def _revalidate(self, results):
        import strategies as slib
        """Per-symbol adaptive tuning for the credit-spread strategies —
        same mechanics and profitability gate as price-action strategies:
        under-trading relaxes entry filters, losing money tightens them,
        and a version only goes LIVE once its OWN backtest is net
        profitable with enough trades to mean something."""
        import backtester, history
        cfg = config.load()
        target = cfg.get("pa_min_trades_per_day", 0.3)
        min_conf = cfg.get("pa_min_trades_for_confidence", 15)
        improve_thresh = cfg.get("pa_tuning_improvement_threshold", 0.15)
        max_attempts = cfg.get("pa_tuning_max_attempts", 4)
        cooldown_days = cfg.get("pa_retune_cooldown_days", 7)
        today_str = now_ist().strftime("%Y-%m-%d")
        vers = backtester.load_versions()
        for sym in self.bus.get("symbols", []):
            total_days = max(1, len(history.chain_days(sym)) or
                             len(history.index_days(sym, 250)))
            m_by_name = results.get(sym) or {}
            for name in ("bull_put_spread", "bear_call_spread"):
                m = m_by_name.get(name) or {}
                entry = backtester._symbol_entry(vers, name, sym)
                if m.get("replay_error"):
                    # v59.74 — a replay that CRASHED is not a replay that
                    # found zero trades: don't refresh results, don't
                    # tune against it, and say so where it will be seen.
                    self.bus.alert("high", self.name, f"{sym}:{name}",
                                   f"replay FAILED — {m['replay_error']} — "
                                   f"results/tuning skipped this run")
                    continue
                for ver in entry["versions"]:
                    if ver["v"] == entry["active"] and m.get("trades") is not None:
                        ver["results"] = m
                        ver["last_tested"] = backtester._now()
                trades = m.get("trades") or 0
                net_pnl = m.get("net_pnl") or 0
                # v59.66 — the sign test steers only the TUNING flow from
                # here on. It set live_enabled for a year and promoted 11
                # of 11 strategies on noise (see is_live_enabled's
                # docstring); the flag itself now comes from the
                # statistical gate, scored on the out-of-sample window
                # only, so the dashboard flag and the alerts below can no
                # longer disagree with what is_live_enabled() would answer.
                profitable_now = trades >= min_conf and net_pnl > 0
                import promotion_gate
                try:
                    gate_ok, gate_d = promotion_gate.evaluate_entry(name, sym, m)
                except Exception as e:
                    gate_ok, gate_d = False, {"reason": f"gate error: {e}"}
                entry["live_enabled"] = bool(gate_ok) and \
                    not entry.get("manually_disabled")
                if profitable_now and not gate_ok:
                    why_not = gate_d.get("reason") or (
                        f"headroom ₹{gate_d.get('headroom'):.0f}/trade"
                        if gate_d.get("headroom") is not None else "gate denied")
                    self.bus.log(self.name,
                                 f"{sym} {name}: net ₹{net_pnl:.0f}/{trades}t "
                                 f"in-sample, but live gate DENIED — {why_not}")
                if not history.chain_days(sym):
                    continue   # spreads need real chain data; nothing to tune yet
                if profitable_now:
                    self.bus.log(self.name,
                                 f"{sym} {name}: v{entry['active']} already "
                                 f"profitable (₹{net_pnl:.0f}/{trades}t) — "
                                 "leaving parameters as-is")
                    continue
                tpd = trades / total_days
                if tpd < target:
                    direction, why = +1, (f"only {tpd:.2f} trades/day over "
                                          f"{total_days}d (target {target}) "
                                          "— relaxing entry filters")
                elif net_pnl < 0 and tpd > target * 2:
                    direction, why = -1, (f"net ₹{net_pnl:.0f} over "
                                          f"{total_days}d at {tpd:.2f} t/d "
                                          "— tightening")
                else:
                    continue
                if entry.get("tuning_exhausted"):
                    next_at = entry.get("next_tune_at")
                    if next_at and today_str < next_at:
                        continue
                    entry["tuning_exhausted"] = False
                    entry["tuning_attempts"] = 0
                last = entry["versions"][-1]
                tuned, changes = slib.tune(name, last["params"], direction)
                if not changes:
                    self.bus.log(self.name,
                                 f"{sym} {name}: {why} but already at filter "
                                 "bound")
                    continue
                # via _replay_for, not replay_spreads directly, so this
                # candidate lands in trial_log — the daily tuner is the
                # path that made N unrecoverable (2026-08-08).
                new_m = backtester.metrics(
                    backtester._replay_for(name, sym, tuned,
                                           source="daily_tune"))
                new_trades = new_m.get("trades") or 0
                new_pnl = new_m.get("net_pnl") or 0
                new_profitable = new_trades >= min_conf and new_pnl > 0
                # v59.67 — a version requires a POSITIVE result that
                # meaningfully improves on the incumbent. The old
                # `new_profitable or meaningful_improvement` accepted a
                # 15%-smaller loss and minted a negative version for it,
                # every manual Run included; 67 of the 85 versions on
                # the live install were non-positive dead weight.
                worth_keeping = backtester.version_worthy(
                    net_pnl, new_pnl, improve_thresh)
                if not worth_keeping:
                    entry["tuning_attempts"] = entry.get("tuning_attempts", 0) + 1
                    self.bus.log(self.name,
                                 f"{sym} {name}: candidate (₹{new_pnl:.0f}) "
                                 f"is not a positive {improve_thresh*100:.0f}%+ "
                                 f"improvement over ₹{net_pnl:.0f} — no version "
                                 f"created (trial_log keeps the record)")
                    if entry["tuning_attempts"] >= max_attempts:
                        from datetime import timedelta
                        entry["tuning_exhausted"] = True
                        entry["next_tune_at"] = (now_ist() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                        self.bus.log(self.name,
                                     f"{sym} {name}: pausing auto-tuning "
                                     f"until {entry['next_tune_at']}")
                    continue
                newv = max(x["v"] for x in entry["versions"]) + 1
                entry["tuning_attempts"] = 0
                entry["versions"].append({
                    "v": newv, "params": tuned,
                    "reason": why + " | " + "; ".join(changes),
                    "created": backtester._now(), "last_tested": backtester._now(),
                    "results": new_m, "deployed": new_profitable})
                if new_profitable and not entry.get("manually_disabled"):
                    entry["active"] = newv
                    # v59.66 — adoption is a PAPER decision; it no longer
                    # implies live. A fresh candidate has zero out-of-
                    # sample days by definition (it was fitted on
                    # everything visible today), so the gate denies it
                    # until the daily backtest attaches a walk-forward
                    # window it survives.
                    try:
                        _gok, _ = promotion_gate.evaluate_entry(name, sym, new_m)
                    except Exception:
                        _gok = False
                    entry["live_enabled"] = bool(_gok)
                    self.bus.alert("medium", self.name, f"{sym}:{name}",
                                   f"{sym} {name} v{newv} adopted for paper "
                                   f"(₹{new_pnl:.0f}/{new_trades}t in-sample) "
                                   "— live stays gated on out-of-sample "
                                   "evidence")
                else:
                    self.bus.log(self.name,
                                 f"{sym} {name} v{newv} kept but NOT "
                                 f"enabled — net ₹{new_pnl:.0f} / {new_trades}t")
            backtester.save_versions(vers)
    def _tune_pa(self, results):
        """Adaptive gating, per (strategy, SYMBOL) independently — one
        index's tuning must never affect another's, since backtests can
        diverge sharply between them. Hard rule: a version is only
        marked live_enabled if its OWN backtest is net PROFITABLE with
        enough trades for the number to mean something; a "smaller loss"
        is never treated as good enough to trade live money on."""
        import backtester, pa_strategies as pa, history
        cfg = config.load()
        target = cfg.get("pa_min_trades_per_day", 0.3)
        min_trades_for_confidence = cfg.get("pa_min_trades_for_confidence", 15)
        improve_thresh = cfg.get("pa_tuning_improvement_threshold", 0.15)
        max_attempts = cfg.get("pa_tuning_max_attempts", 4)
        cooldown_days = cfg.get("pa_retune_cooldown_days", 7)
        today_str = now_ist().strftime("%Y-%m-%d")
        vers = backtester.load_versions()
        for sym in self.bus.get("symbols", []):
            total_days = max(1, len(history.index_days(sym, 250)))
            if total_days < 5:
                continue
            for name in pa.PA_NAMES:
                m = (results.get(sym) or {}).get(name) or {}
                entry = backtester._symbol_entry(vers, name, sym)
                if m.get("replay_error"):
                    # v59.74 — a replay that CRASHED is not a replay that
                    # found zero trades: don't refresh results, don't
                    # tune against it, and say so where it will be seen.
                    self.bus.alert("high", self.name, f"{sym}:{name}",
                                   f"replay FAILED — {m['replay_error']} — "
                                   f"results/tuning skipped this run")
                    continue
                # keep the active version's stored results current, so the
                # dashboard never shows "not yet backtested" once we have
                # real numbers, and so the profitability check below uses
                # the freshest data even when no new version is proposed
                for ver in entry["versions"]:
                    if ver["v"] == entry["active"] and m.get("trades") is not None:
                        ver["results"] = m
                        ver["last_tested"] = backtester._now()
                trades = m.get("trades") or 0
                net_pnl = m.get("net_pnl") or 0
                # v59.66 — sign test steers tuning only; live_enabled
                # comes from the statistical gate on the out-of-sample
                # window. See the identical change in _revalidate above.
                profitable_now = trades >= min_trades_for_confidence and net_pnl > 0
                import promotion_gate
                try:
                    gate_ok, gate_d = promotion_gate.evaluate_entry(name, sym, m)
                except Exception as e:
                    gate_ok, gate_d = False, {"reason": f"gate error: {e}"}
                entry["live_enabled"] = bool(gate_ok) and \
                    not entry.get("manually_disabled")
                if profitable_now and not gate_ok:
                    why_not = gate_d.get("reason") or (
                        f"headroom ₹{gate_d.get('headroom'):.0f}/trade"
                        if gate_d.get("headroom") is not None else "gate denied")
                    self.bus.log(self.name,
                                 f"{sym} {name}: net ₹{net_pnl:.0f}/{trades}t "
                                 f"in-sample, but live gate DENIED — {why_not}")
                if profitable_now:
                    # already working — don't keep tuning for more frequency
                    # at the risk of eroding a proven edge
                    self.bus.log(self.name,
                                 f"{sym} {name}: v{entry['active']} already "
                                 f"profitable (₹{net_pnl:.0f}/{trades}t) — "
                                 "leaving parameters as-is")
                    continue
                tpd = trades / total_days
                if tpd < target:
                    direction, why = +1, (f"only {tpd:.2f} trades/day over "
                                          f"{total_days}d (target {target}) "
                                          "— relaxing filters")
                elif net_pnl < 0 and tpd > target * 2:
                    direction, why = -1, (f"net ₹{net_pnl:.0f} over "
                                          f"{total_days}d at {tpd:.2f} t/d "
                                          "— tightening")
                else:
                    backtester.save_versions(vers)
                    continue
                # boundary: stop retrying every single day once several
                # attempts show no real improvement — wait out a cooldown
                # instead of spawning an endless chain of versions
                if entry.get("tuning_exhausted"):
                    next_at = entry.get("next_tune_at")
                    if next_at and today_str < next_at:
                        continue
                    entry["tuning_exhausted"] = False
                    entry["tuning_attempts"] = 0
                last = entry["versions"][-1]
                tuned, changes = pa.tune(name, last["params"], direction)
                if not changes:
                    self.bus.log(self.name,
                                 f"{sym} {name}: {why} but already at filter "
                                 "bound — no further tuning possible")
                    backtester.save_versions(vers)
                    continue
                # via _replay_for — see the note at the spread tuner above.
                new_m = backtester.metrics(
                    backtester._replay_for(name, sym, tuned,
                                           source="daily_tune"))
                new_trades = new_m.get("trades") or 0
                new_pnl = new_m.get("net_pnl") or 0
                new_profitable = (new_trades >= min_trades_for_confidence
                                  and new_pnl > 0)
                # v59.67 — same rule as _revalidate: no version without a
                # positive, meaningfully-improved result.
                worth_keeping = backtester.version_worthy(
                    net_pnl, new_pnl, improve_thresh)
                if not worth_keeping:
                    entry["tuning_attempts"] = entry.get("tuning_attempts", 0) + 1
                    self.bus.log(self.name,
                                 f"{sym} {name}: candidate (₹{new_pnl:.0f}) "
                                 f"is not a positive {improve_thresh*100:.0f}%+ "
                                 f"improvement over ₹{net_pnl:.0f} — no version "
                                 f"created (attempt {entry['tuning_attempts']}/"
                                 f"{max_attempts})")
                    if entry["tuning_attempts"] >= max_attempts:
                        from datetime import timedelta
                        entry["tuning_exhausted"] = True
                        entry["next_tune_at"] = (now_ist() + timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
                        self.bus.log(self.name,
                                     f"{sym} {name}: no improvement after "
                                     f"{max_attempts} attempts — pausing "
                                     f"auto-tuning until {entry['next_tune_at']}")
                    backtester.save_versions(vers)
                    continue
                newv = max(x["v"] for x in entry["versions"]) + 1
                entry["tuning_attempts"] = 0
                entry["versions"].append({
                    "v": newv, "params": tuned,
                    "reason": why + " | " + "; ".join(changes),
                    "created": backtester._now(), "last_tested": backtester._now(),
                    "results": new_m, "deployed": new_profitable})
                if new_profitable:
                    entry["active"] = newv
                    # v59.66 — same as the spread tuner: adoption is a
                    # paper decision, live waits for out-of-sample days.
                    try:
                        _gok, _ = promotion_gate.evaluate_entry(name, sym, new_m)
                    except Exception:
                        _gok = False
                    entry["live_enabled"] = bool(_gok)
                    self.bus.alert("medium", self.name, f"{sym}:{name}",
                                   f"{sym} {name} v{newv} adopted for paper "
                                   f"(₹{new_pnl:.0f}/{new_trades}t in-sample) "
                                   "— live stays gated on out-of-sample "
                                   "evidence")
                else:
                    self.bus.log(self.name,
                                 f"{sym} {name} v{newv} kept "
                                 f"({improve_thresh*100:.0f}%+ improvement to "
                                 f"₹{new_pnl:.0f}) but still not profitable "
                                 "enough for live trading")
                backtester.save_versions(vers)


def build_pa_signal(name, ev, entry, leg, row, analysis, p, risk_pct, s7_gates=None):
    """v57.1 — extracted verbatim from PriceActionAgent.cycle()'s inline
    sig-construction block (byte-identical math, not a rewrite) so the
    new manual-deploy endpoint (/api/strategies/manual_fire) can build
    the EXACT same signal the automatic loop would, rather than a
    second copy that could silently drift from it. Only sg_ema
    overrides target1/target2 with its own rr_target and attaches
    s7_gates/setup — every other PA strategy uses the plain rr=2.0/2.67
    shape unchanged."""
    sig = {"signal": "BUY_CE" if ev["dir"] > 0 else "BUY_PE",
           "strike": analysis["atm"], "entry": entry,
           "stoploss": round(entry * (1 - risk_pct), 2),
           "target1": round(entry * (1 + risk_pct * 2), 2),
           "target2": round(entry * (1 + risk_pct * 2.67), 2),
           "spot_invalidation": round(ev["stop_spot"], 1),
           "confidence": 74, "timeframe": "intraday",
           "security_id": row[leg].get("security_id"),
           "reasons": [f"[{name}] {ev['why']}"],
           "source": name,
           # v58.47 — names a LIVE condition for the monitor loop to
           # evaluate on every future candle, not a price level. Only
           # momentum_confluence uses it today; see
           # agents.dynamic_exit_reason().
           "dynamic_exit": ("macd_hist_turn"
                            if name == "momentum_confluence" else None)}
    if name == "sg_ema":
        rr = p.get("rr_target", 2.0)
        sig["target1"] = round(entry * (1 + risk_pct * rr), 2)
        sig["target2"] = round(entry * (1 + risk_pct * rr * 1.33), 2)
        sig["s7_gates"] = s7_gates
        sig["setup"] = "sg_ema"
    elif name == "ew_reversal":
        # v58.28 (Strategy 8) — same rr-override shape as sg_ema above.
        # `s7_gates` is reused as the carrier for S8's per-detector
        # breakdown purely because build_pa_signal()'s signature already
        # has that slot and every caller already passes it; the payload
        # is surfaced under its own key (`s8_detectors`) so nothing
        # downstream can confuse an S8 detector map for an S7 gate map.
        rr = p.get("rr_target", 2.0)
        sig["target1"] = round(entry * (1 + risk_pct * rr), 2)
        sig["target2"] = round(entry * (1 + risk_pct * rr * 1.33), 2)
        sig["s8_detectors"] = s7_gates
        sig["setup"] = "ew_reversal"
        sig["setup_subtype"] = ev.get("setup_subtype")
        sig["reasons"] = [f"[S8/{ev.get('setup_subtype')}] {ev['why']}"]
    return sig


class PriceActionAgent(Agent):
    """Live ORB / anchor-pullback / EMA-MTF setups on session candles.
    Emits standard BUY_CE/BUY_PE signals into the normal risk pipeline —
    capital gates (loss limits, caps, cooldowns) always apply; only the
    direction filters are adaptively tuned by the backtest agent."""
    name, interval = "price_action", 60

    def cycle(self):
        import backtester, pa_strategies as pa
        if not market_open():
            self.summary = "market closed"
            return
        cfg = config.load()
        enabled = cfg.get("pa_enabled", list(pa.PA_NAMES))
        # v58.28 — Strategy 8 was added AFTER pa_enabled was first
        # persisted to disk. On any existing install the saved list
        # predates "ew_reversal" and therefore silently excludes it, so
        # a membership check alone would leave S8 permanently dead with
        # no error anywhere — the exact drift the v55.1 note above this
        # key in config.py warns about, which has already bitten this
        # project twice (sg_ema, then momentum_confluence). S8 is gated
        # on its OWN master switch instead of on membership in a list
        # written before it existed.
        if cfg.get("strategy8_enabled", True) and "ew_reversal" not in enabled:
            enabled = list(enabled) + ["ew_reversal"]
        if not enabled:
            self.summary = "disabled (pa_enabled empty)"
            return
        if not hasattr(self, "_taken"):
            self._taken, self._cool, self._day = {}, {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._cool, self._day = {}, {}, today
        fired = []
        skipped = {"no_position_free": 0, "stale_or_missing_pack": 0,
                  "no_analysis": 0, "on_cooldown": 0, "no_setup": 0}
        # Per-strategy breakdown added 2026-07-24: the aggregate
        # "no_setup" counter above made it structurally impossible to
        # tell WHICH of orb/vwap_pullback/ema_mtf was actually silent —
        # exactly the question this was built to answer. Kept the
        # aggregate too (existing consumers may read skipped["no_setup"]).
        no_setup_by_strategy = {name: 0 for name in enabled}
        positions = self.bus.get("positions", {}) or {}
        for sym in self.bus.get("symbols", []):
            if sym in positions:
                skipped["no_position_free"] += 1
                continue
            pack = self.bus.get(f"pa_candles:{sym}")
            if not pack or time.time() - pack["ts"] > 240:
                skipped["stale_or_missing_pack"] += 1
                continue
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis:
                skipped["no_analysis"] += 1
                continue
            for name in enabled:
                key = f"{sym}:{name}"
                if time.time() - self._cool.get(key, 0) < 1800:
                    skipped["on_cooldown"] += 1
                    continue
                if not cfg["paper_mode"] and not backtester.is_live_enabled(name, sym):
                    continue   # not yet proven profitable in backtest for THIS symbol
                p = backtester.get_params(name, sym)
                # Bug found 2026-07-24 from live logs: ema_mtf never fired
                # a single signal, on any day — not a market-conditions
                # issue as it first looked (vwap_pullback's confluence
                # rejections that same session made it plausible). Root
                # cause: c15_today was computed above but never stored in
                # pa_candles:{sym} (only c1/c5 were), AND this call passed
                # a hardcoded None as the 15-min candles argument
                # regardless. ema_mtf's mtf_confirm (the DEFAULT setting)
                # requires both c5 AND c15 to be present — with c15
                # always None, it bailed at that check on every single
                # call, permanently, independent of whether a real 5/13
                # EMA cross was happening. Both the missing storage and
                # this hardcoded None are now fixed.
                s7_gates = None
                if name == "sg_ema":
                    # Strategy 7 (v51): explicit master switch + paper-
                    # mode hard gate (matching the futures Phase-1
                    # precedent — no live path in v51), evaluated with
                    # the ZigZag pivots the CHART draws (structure.
                    # zigzag_series on today's 1m — the spec's parity
                    # requirement) and the Feature #2 bias payload.
                    if not cfg.get("strategy7_enabled", True):
                        continue
                    if not cfg.get("s7_auto_deploy", False):
                        continue      # eligibility still visible via API
                    if not cfg.get("paper_mode", True):
                        continue      # v51: paper-only, hard gate
                    import structure
                    pivots = structure.zigzag_series(pack["c1"])
                    # config keys override the PA defaults (registered in
                    # config.DEFAULTS — see the save()-drops-keys note)
                    p = dict(p,
                             fast=cfg.get("s7_ema_fast", 5),
                             slow=cfg.get("s7_ema_slow", 13),
                             mtf_confirm=cfg.get("s7_mtf_confirm", 1),
                             require_structure=1 if cfg.get("s7_require_structure", True) else 0,
                             require_ai_bias=1 if cfg.get("s7_require_ai_bias", True) else 0,
                             min_ai_bias=cfg.get("s7_min_ai_bias", 20),
                             structural_stop_buffer_pct=cfg.get("s7_structural_stop_buffer_pct", 0.05),
                             rr_target=cfg.get("s7_rr_target", 2.0),
                             max_trades_per_day=cfg.get("s7_max_trades_per_day", 2))
                    ev, s7_gates = pa.evaluate_sg_ema(
                        pack["c1"], pack["c5"], pack.get("c15"),
                        params=p, taken_today=self._taken.get(key, 0),
                        pivots=pivots, ai_bias=self.bus.get(f"bias:{sym}"))
                    self.bus.set(f"s7_gates:{sym}", s7_gates)
                    # 2026-07-27 (item 11) — rejected-signal markers: the
                    # client already had a dedicated marker layer for
                    # this (`lwSignalMarkers`, its own comment naming
                    # "entries/exits/rejections" since v51) and a
                    # Settings toggle (`s7_show_rejected_markers`)
                    # already existed — but NEITHER side ever actually
                    # wired a rejection through; confirmed genuinely
                    # dead code on both ends, not "client ready, server
                    # not wired" as an earlier note assumed. A
                    # "rejection" worth marking is specifically a real
                    # EMA cross that a LATER gate then blocked (cross
                    # True, one of mtf/structure/ai_bias explicitly
                    # False) — not the routine "no cross at all" case,
                    # which is most cycles and isn't a near-miss worth
                    # flagging. Same transition-based approach already
                    # proven for the False Breakout marker fix (item 4):
                    # only mark a NEW rejection, not the same blocked
                    # state re-announced every cycle.
                    if cfg.get("s7_markers_enabled", True) and \
                            cfg.get("s7_show_rejected_markers", False):
                        blocked_gate = next(
                            (g for g in ("mtf", "structure", "ai_bias")
                            if s7_gates.get(g) is False), None)
                        rej_key = f"{sym}:s7_rejection"
                        if not hasattr(self, "_s7_rejection_state"):
                            self._s7_rejection_state = {}
                        was_active = self._s7_rejection_state.get(rej_key, False)
                        is_active = bool(s7_gates.get("cross")) and blocked_gate is not None
                        self._s7_rejection_state[rej_key] = is_active
                        if is_active and not was_active:
                            spot = analysis.get("spot")
                            if spot is not None:
                                self._record_s7_rejection(sym, blocked_gate, spot)
                elif name == "ew_reversal":
                    # Strategy 8 (v58.28) — EW-Reversal. Same two-key
                    # posture as S7: master switch + auto-deploy, plus
                    # the paper-mode hard gate, so eligibility stays
                    # visible via the API while the strategy cannot
                    # fire a real order.
                    # v58.37 — the auto_deploy gate used to sit HERE,
                    # before evaluation, so with it off (the default,
                    # and how S8 has shipped since v58.28) the strategy
                    # was never evaluated at all: no eligibility, no
                    # Shadow Journal, no per-detector counts. A live log
                    # proved it — every other PA strategy reported 4
                    # no-setup counts per cycle (one per symbol) while
                    # ew_reversal reported 0, because it never ran.
                    #
                    # This is the SAME mistake diagnosed and fixed for
                    # Strategy 9 in v58.32 (confluence computed inside
                    # the auto_deploy gate, so observing with it off
                    # captured nothing) and left uncorrected here.
                    # Gates now govern TRADING only, never observation.
                    if not cfg.get("strategy8_enabled", True):
                        continue
                    import structure, ew_reversal
                    # The CHART's own ZigZag (parity requirement) — the
                    # identical call S7 makes, same deviation default.
                    pivots = structure.zigzag_series(
                        pack["c1"], cfg.get("s8_zigzag_deviation_pct", 0.5))
                    p = dict(p,
                             min_pattern_bars=cfg.get("s8_min_pattern_bars", 12),
                             shoulder_tol_pct=cfg.get("s8_shoulder_tol_pct", 1.5),
                             neckline_buffer_pct=cfg.get("s8_neckline_buffer_pct", 0.05),
                             stop_buffer_pct=cfg.get("s8_stop_buffer_pct", 0.05),
                             require_macd_divergence=1 if cfg.get("s8_require_macd_divergence", True) else 0,
                             require_tide=1 if cfg.get("s8_require_tide", True) else 0,
                             ending_diagonal_enabled=1 if cfg.get("s8_ending_diagonal_enabled", True) else 0,
                             hs_enabled=1 if cfg.get("s8_hs_enabled", True) else 0,
                             failed_hs_enabled=1 if cfg.get("s8_failed_hs_enabled", True) else 0,
                             rr_target=cfg.get("s8_rr_target", 2.0),
                             max_trades_per_day=cfg.get("s8_max_trades_per_day", 2))
                    # Exception isolation: S8 is brand new and shares
                    # this loop with six strategies that are already
                    # trading. An unhandled error here would abort the
                    # whole cycle for ALL of them, so it is caught and
                    # logged LOUDLY (with the exception type, so a real
                    # code bug is distinguishable from a data gap —
                    # this project's fail-loud rule) while the other
                    # strategies continue their cycle unaffected.
                    try:
                        # v58.29 — the deck is explicit that an H&S
                        # FAILS when the Tide is against it, but v58.28
                        # only consulted the Tide inside failed_hs, so
                        # a plain H&S could fire a short into an up
                        # Tide. s8_require_tide_all_detectors closes
                        # that, and the Tide itself is read from
                        # TAElliottAgent's published state when
                        # available rather than recomputed here.
                        shared = self.bus.get(f"ta_state:{sym}") or {}
                        ev, s7_gates = ew_reversal.evaluate(
                            pack["c1"], pack["c5"], pack.get("c15"),
                            params=p, taken_today=self._taken.get(key, 0),
                            pivots=pivots,
                            shared_tide=(shared.get("tide")
                                         if cfg.get("s8_use_shared_tide", True)
                                         else None))
                        self.bus.set(f"s8_detectors:{sym}", s7_gates)
                    except Exception as e:
                        self.bus.log(self.name,
                                     f"{sym}: S8 ew_reversal FAILED "
                                     f"({type(e).__name__}: {e}) — other "
                                     "strategies unaffected this cycle")
                        continue
                    # Detector outcomes are published every cycle so the
                    # Strategies page and any future calibration log can
                    # read them whether or not S8 may trade.
                    self.bus.set(f"s8_eligibility:{sym}",
                                 {"eligible": bool(ev), "detectors": s7_gates,
                                  "why": (ev or {}).get("why")})
                    if not cfg.get("s8_auto_deploy", False):
                        continue      # observed and published above; cannot trade
                    if not cfg.get("paper_mode", True):
                        continue      # paper-only on introduction
                else:
                    ev = pa.evaluate(name, pack["c1"], pack["c5"],
                                     pack.get("c15"), params=p,
                                     taken_today=self._taken.get(key, 0))
                if not ev:
                    skipped["no_setup"] += 1
                    no_setup_by_strategy[name] = no_setup_by_strategy.get(name, 0) + 1
                    continue
                leg = "ce" if ev["dir"] > 0 else "pe"
                row = next((r for r in analysis.get("strikes", [])
                            if r["strike"] == analysis.get("atm")), None)
                entry = row and row[leg].get("ltp")
                if not entry:
                    continue
                # Risk distance: fixed 15% by default, or ATR-scaled when
                # stop_mode="atr" (uses the regime engine's already-computed
                # atr_pct — underlying ATR as % of spot — scaled onto the
                # option premium via atr_stop_multiplier, since a live
                # per-strike ATR series isn't maintained). Clamped to a
                # 5-30% band so a near-zero or extreme ATR reading can't
                # produce a degenerate stop. target1 is kept at EXACTLY
                # 2x the risk distance (rr=2.0) regardless of mode — the
                # RiskAgent's risk-reward gate requires rr>=1.95, and a
                # naive fixed-ratio-independent-of-stop version would
                # silently reject every ATR-mode signal the same way an
                # earlier 20%/25% attempt did for the fixed-pct version.
                if cfg.get("stop_mode", "fixed_pct") == "atr":
                    regime = self.bus.get(f"regime:{sym}") or {}
                    atr_pct = regime.get("atr_pct")
                    if atr_pct:
                        risk_pct = min(0.30, max(0.05, atr_pct * cfg.get(
                            "atr_stop_multiplier", 2.5) / 100))
                    else:
                        risk_pct = 0.15  # no ATR reading yet — fixed fallback
                else:
                    risk_pct = 0.15
                if name in ("sg_ema", "ew_reversal") and \
                        ev.get("structural_stop") is not None:
                    # Strategy 7: premium risk derived from the STRUCTURAL
                    # spot distance (last confirmed pivot), translated to
                    # the premium via the same 0.5-delta approximation the
                    # backtest replay documents. Clamped 5-30% exactly
                    # like the ATR mode — the spec's own open question
                    # ("pivot very far away: clamp or skip?") resolved as
                    # CLAMP, matching how the ATR stop already handles a
                    # degenerate reading rather than silently dropping
                    # the signal. rr_target then scales the targets off
                    # the CLAMPED risk, so the >=1.95 risk-reward gate
                    # can never auto-reject a structurally-wide stop.
                    spot_risk_pct = abs(ev["entry_spot"] - ev["structural_stop"]) \
                        / max(ev["entry_spot"], 1e-9)
                    prem_risk = spot_risk_pct * ev["entry_spot"] * 0.5 / max(entry, 1e-9)
                    risk_pct = min(0.30, max(0.05, prem_risk))
                sig = build_pa_signal(name, ev, entry, leg, row, analysis, p, risk_pct, s7_gates)
                self._taken[key] = self._taken.get(key, 0) + 1
                self._cool[key] = time.time()
                fired.append(f"{sym} {name} {sig['signal']}")
                self.bus.log(self.name,
                             f"{sym}: {name} -> {sig['signal']} ({ev['why']})")
                self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                            "analysis": analysis})
        self.summary = " · ".join(fired) if fired else \
            f"scanning {len(enabled)} setups across symbols ({skipped}, " \
            f"by strategy: {no_setup_by_strategy})"
        # Diagnostic breadcrumb every ~10 min when nothing fired — this is
        # exactly the visibility gap that made "why didn't ORB/vwap/ema
        # ever fire today" impossible to answer from the logs alone.
        if not fired and time.time() - getattr(self, "_last_diag_log", 0) > 600:
            self._last_diag_log = time.time()
            self.bus.log(self.name, f"no PA signals this cycle — {skipped} "
                         f"(no-setup by strategy: {no_setup_by_strategy})")


    def _record_s7_rejection(self, symbol, blocked_gate, spot):
        """v58.9 (item 11) — persists a rejected-signal marker event,
        the missing half of the "rejected-signal markers" follow-up:
        confirmed entries are shown via the SAME generic chart-events
        mechanism every strategy already uses (agents.py's own
        `_record_chart_event`); this is specifically for a real EMA
        cross that a LATER gate then blocked — a near-miss worth
        seeing, not a confirmed trade. Mirrors the SAME persist-and-
        day-prune pattern already proven for the institutional-event
        markers fix (item 4) — capped list, pruned to today's entries
        only each time, so a stale rejection from a prior day can't
        leak forward the same way the False Breakout marker did before
        that fix."""
        events = self.bus.get(f"s7_rejected_events:{symbol}", [])
        today_str = now_ist().date().isoformat()
        events = [e for e in events if e.get("day") == today_str]
        events.append({"time": int(time.time()), "gate": blocked_gate,
                       "spot": spot, "day": today_str})
        self.bus.set(f"s7_rejected_events:{symbol}", events[-30:])


class MTFConfluenceAgent(Agent):
    """MACD+Stoch Confluence strategy (rinkoo.docx, 2026-07-23).
    Daily/weekly MTF confluence on MACD/RSI/Stochastic/Bollinger Bands
    -> BUY_CE/BUY_PE signal into the standard risk pipeline (same
    capital gates, position caps, daily loss limit etc. as every other
    signal source — this strategy gets no special exemption).

    Runs every 15 min during market hours — daily/weekly data doesn't
    change intraday, so this cadence is already far more often than
    the underlying data can meaningfully change; it just keeps the
    strategy responsive to a fresh signal appearing without hammering
    the historical-data endpoint (which also self-caches 6h internally
    in broker_adapter.py regardless).

    Requires Dhan as the active broker — historical_daily() (true
    daily-timeframe candles, needed for weekly MACD resampling) only
    exists on DhanClient today. Degrades to a clear "requires Dhan"
    status rather than erroring for other brokers.
    """
    name, interval = "mtf_confluence", 900

    def _announce_once(self, msg):
        """Log a status line once per calendar day.

        Silence from an agent is ambiguous — it could mean "nothing to
        report" or "never ran". This makes the activity log say which,
        without repeating on every 15-minute cycle.
        """
        today = now_ist().strftime("%Y-%m-%d")
        if getattr(self, "_announced_day", None) == today:
            return
        self._announced_day = today
        self.bus.log(self.name, msg)

    def cycle(self):
        import mtf_confluence_strategy as mcs
        cfg = config.load()
        if not cfg.get("mtf_confluence_enabled", True):
            self.summary = "disabled (Settings -> Strategies)"
            return
        if not market_open():
            self.summary = "market closed"
            return
        self._announce_once("active — Dhan historical_daily available; "
                            "silence after this line means no qualifying "
                            "setup, not an inactive agent")
        if cfg.get("broker", "dhan") != "dhan":
            # 2026-07-29 — this agent logged ZERO lines across a full
            # session, so its silence was ambiguous: working and quiet,
            # or silently unavailable? A summary string only shows on
            # the Agents page if someone looks. State it in the log
            # ONCE per session so the activity log answers the question
            # on its own.
            self.summary = "requires Dhan (historical_daily not yet built for other brokers)"
            self._announce_once("requires Dhan as the active broker — "
                                "historical_daily() only exists on DhanClient, "
                                "so this strategy is INACTIVE this session")
            return
        dc = self.ctx.get("dhan_client")
        d = dc() if dc else None
        if d is None:
            self.summary = "no Dhan client available"
            # Worth a periodic log entry (not just self.summary) since
            # this is a genuinely surprising failure when broker=dhan
            # and the strategy is enabled — every other gate here
            # (disabled/market-closed/wrong-broker) is self-explanatory
            # and expected some of the time; this one specifically
            # means something is actually wrong with the broker client.
            if time.time() - getattr(self, "_last_diag_log", 0) > 1800:
                self._last_diag_log = time.time()
                self.bus.log(self.name, "no Dhan client available — "
                             "mtf_confluence_enabled and broker=dhan are "
                             "both set, but the client factory returned "
                             "nothing (check the Dhan connection itself)")
            return
        if not hasattr(self, "_taken"):
            self._taken, self._day = {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._day = {}, today

        positions = self.bus.get("positions", {}) or {}
        max_per_day = cfg.get("mtf_max_trades_per_day", 1)
        min_conf = cfg.get("mtf_min_confidence", 70)
        results = {}
        for sym in self.bus.get("symbols", []):
            results[sym] = self._evaluate_and_fire(sym, d, cfg, positions, max_per_day, min_conf)
        self.summary = "; ".join(f"{k}: {v}" for k, v in results.items()) or "no symbols configured"
        # Diagnostic breadcrumb added 2026-07-24: this agent previously
        # only wrote to the activity log when it actually fired a
        # signal — meaning a full day of silence gave zero visibility
        # into WHY (no qualifying setup all day — plausible, this is a
        # deliberately demanding 5-condition confluence — vs. a data/
        # config problem silently preventing evaluation entirely, e.g.
        # historical_daily() failing, or the broker/enabled gates
        # tripping). Same "why is X silent" gap already fixed for PA
        # strategies and spread auto-deploy; this agent had been missed.
        if not any("FIRED" in v for v in results.values()) and \
                time.time() - getattr(self, "_last_diag_log", 0) > 1800:
            self._last_diag_log = time.time()

    def _evaluate_and_fire(self, sym, d, cfg, positions=None, max_per_day=None, min_conf=None):
        """v57.2 — extracted verbatim from the per-symbol body that used
        to sit inline in cycle()'s loop (byte-identical math, not a
        rewrite), so a manual-deploy endpoint can call this SAME method
        for one symbol on demand rather than duplicating the entry/
        stop/target formula a third time. `positions`/`max_per_day`/
        `min_conf` default-compute from live state/config when omitted
        (the manual-fire caller doesn't loop, so it doesn't already
        have these on hand the way cycle() does) — but when a manual
        caller WANTS the exact real-time gates cycle() itself enforces
        (position-open, daily-count, confidence), it should pass them
        through exactly as cycle() does, which the API endpoint does.
        Returns a human-readable outcome string; also mutates self.
        _taken and publishes the signal exactly like the automatic
        loop, so a manual fire counts toward the same daily cap and
        can't be double-fired moments later by the automatic cycle."""
        import mtf_confluence_strategy as mcs
        if positions is None:
            positions = self.bus.get("positions", {}) or {}
        if max_per_day is None:
            max_per_day = cfg.get("mtf_max_trades_per_day", 1)
        if min_conf is None:
            min_conf = cfg.get("mtf_min_confidence", 70)
        if not hasattr(self, "_taken"):
            self._taken, self._day = {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._day = {}, today
        if sym in positions:
            return "position open"
        if self._taken.get(sym, 0) >= max_per_day:
            return "max trades/day reached"
        try:
            candles = d.historical_daily(sym)["candles"]
        except Exception as e:
            return f"data fetch failed: {e}"
        future_buildup = self.bus.get(f"future_oi_trend:{sym}")
        global_sentiment = self.bus.get("global_risk_sentiment")
        result = mcs.evaluate(candles, future_buildup=future_buildup,
                              global_sentiment=global_sentiment)
        self.bus.set(f"mtf_confluence:{sym}", result)
        if not result:
            return "no confluence"
        if result["confidence"] < min_conf:
            return f"confidence {result['confidence']} < {min_conf}"
        analysis = self.bus.get(f"chain:{sym}")
        if not analysis or not analysis.get("rows") or not analysis.get("spot"):
            return "no chain data yet"
        atm = min(analysis["rows"], key=lambda r: abs(r["strike"] - analysis["spot"]))
        leg = "ce" if result["direction"] == "bullish" else "pe"
        entry = atm[leg].get("ltp")
        if not entry:
            return "no ATM ltp"
        atr = result["daily_atr14"] or 0
        sl_pts_index = 1.5 * atr if atr else entry * 0.30
        # 2026-08-02 — routed through analyzer.option_stop_geometry(), the
        # single definition. This site used its OWN clamp ([10%, 60%])
        # while two other paths used [5%, 30%] and a third used a flat
        # 30%, which is what produced discrete stop widths in the journal.
        from analyzer import option_stop_geometry as _osg
        _atr_pct = (100.0 * atr / analysis["spot"]) if (atr and analysis.get("spot")) else None
        # cfg=None so the helper loads it: `webhook_signal` (the other
        # call site) has no `cfg` in scope, and passing one there would
        # NameError at runtime while compiling cleanly.
        stoploss, _t1, _t2, _sg_meta = _osg(entry, None, atr_pct=_atr_pct,
                                            spot=analysis.get("spot"))
        sl_pts_premium = entry - stoploss
        risk = entry - stoploss
        if risk <= 0:
            return "degenerate stop distance"
        target1 = round(entry + risk * 2, 2)
        target2 = round(entry + risk * 2.67, 2)
        sig = {
            "signal": "BUY_CE" if result["direction"] == "bullish" else "BUY_PE",
            "strike": atm["strike"], "entry": entry,
            "stoploss": stoploss, "target1": target1, "target2": target2,
            "confidence": result["confidence"], "timeframe": "swing",
            "security_id": atm[leg].get("security_id"),
            "reasons": [f"[mtf_confluence] {r}" for r in result["reasons"]],
            "source": "mtf_confluence",
            "atr": atr,
            "also_consider": mcs.RECOMMENDED_ACTIONS.get(result["direction"], []),
        }
        self._taken[sym] = self._taken.get(sym, 0) + 1
        self.bus.log(self.name,
                    f"{sym}: MTF confluence {result['direction']} "
                    f"(confidence {result['confidence']}) -> {sig['signal']} "
                    f"@ {atm['strike']} — also consider: "
                    f"{', '.join(sig['also_consider'])}")
        self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                    "analysis": analysis})
        return f"FIRED {sig['signal']} conf={result['confidence']}"



class TAElliottAgent(Agent):
    """Strategy 9 (v58.29) — "TA with Elliott".

    Its OWN agent, deliberately, for two reasons the brief named:

    PERFORMANCE. compute_state() builds twelve GMMA EMAs, Bollinger
    bands, Wilder ADX/RSI, MACD and a 5m pivot series per symbol.
    PriceActionAgent already loops six strategies across every symbol
    on a 60s cycle; folding this in would multiply that cycle's cost
    for a strategy whose inputs are 5m/15m candles and therefore cannot
    change more than once every few minutes. This agent runs on 180s
    instead, and ta_elliott.compute_state() is memoised on the last
    candle timestamp so repeat calls inside one candle are free.

    INTERACTION. The computed state is PUBLISHED to the bus as
    `ta_state:{sym}` rather than kept private, so other agents read it
    instead of recomputing the same indicators:
      - PriceActionAgent's Strategy 8 (ew_reversal) reads `tide` from
        here when s8_use_shared_tide is on, instead of building its own
        15m EMA stack.
      - `route` ("BUY_OPTIONS" / "SPREADS" / "NO_TRADE") is published
        as a hint for any agent that wants the deck's impulse-vs-
        corrective read. Published, never enforced — this agent does
        not veto anyone else's signal.

    Signals go into the SAME risk pipeline as every other source; this
    strategy gets no exemption from capital gates, loss limits or
    position caps.
    """
    name, interval = "ta_elliott", 180

    def cycle(self):
        import backtester, ta_elliott, structure
        cfg = config.load()
        if not cfg.get("ta_elliott_enabled", True):
            self.summary = "disabled (ta_elliott_enabled off)"
            return
        if not market_open():
            self.summary = "market closed"
            return
        if not hasattr(self, "_taken"):
            self._taken, self._cool, self._day = {}, {}, None
        today = now_ist().strftime("%Y-%m-%d")
        if self._day != today:
            self._taken, self._cool, self._day = {}, {}, today

        p = dict(backtester.get_params("ta_elliott", "_global"),
                 min_confluence=cfg.get("ta_min_confluence", 3),
                 require_tide=1 if cfg.get("ta_require_tide", True) else 0,
                 bb_period=cfg.get("ta_bb_period", 20),
                 bb_stdev=cfg.get("ta_bb_stdev", 2.0),
                 bb_slope_eps=cfg.get("ta_bb_slope_eps", 0.0004),
                 gmma_compression_pct=cfg.get("ta_gmma_compression_pct", 25.0),
                 gmma_timeframe=cfg.get("ta_gmma_timeframe", "1m"),
                 adx_dynamic_min=cfg.get("ta_adx_dynamic_min", 20.0),
                 rsi_period=cfg.get("ta_rsi_period", 14),
                 zigzag_deviation_pct=cfg.get("ta_zigzag_deviation_pct", 0.5),
                 stop_buffer_pct=cfg.get("ta_stop_buffer_pct", 0.05),
                 rr_target=cfg.get("ta_rr_target", 2.0),
                 require_corrective_phase=1 if cfg.get("ta_require_corrective_phase", False) else 0,
                 tide_use_15m=1 if cfg.get("ta_tide_use_15m", False) else 0,
                 max_trades_per_day=cfg.get("ta_max_trades_per_day", 2))

        positions = self.bus.get("positions", {}) or {}
        # 2026-08-04 — `no_pack` used to mean BOTH "RegimeAgent never
        # published a pack" and "it published one over 240s ago". Those
        # have different causes and different fixes: absent means the
        # producer bailed (stale session date, or fewer than 3 5m bars),
        # stale means it is publishing too SLOWLY for this agent's
        # freshness window. Live on 2026-08-04, S9 wrote exactly ONE 5m
        # candle all session and then reported no_pack=4 for the rest of
        # it, and the counter could not say which of the two it was —
        # the third instance this week of a signal that collapses two
        # states into one. The worst observed age is carried into the log
        # so "stale" comes with the number that would fix it.
        fired, phases, observed = [], {}, []
        skipped = {"pack_absent": 0, "pack_stale": 0, "no_analysis": 0,
                   "state_not_ok": 0, "no_setup": 0,
                   "position_open": 0, "on_cooldown": 0}
        worst_age = 0.0
        for sym in self.bus.get("symbols", []):
            pack = self.bus.get(f"pa_candles:{sym}")
            if not pack:
                skipped["pack_absent"] += 1
                continue
            _age = time.time() - pack["ts"]
            if _age > 240:
                skipped["pack_stale"] += 1
                worst_age = max(worst_age, _age)
                continue
            # State is computed and PUBLISHED even when this symbol
            # can't be traded right now (position open, cooldown) —
            # other agents consume `ta_state:{sym}` and must not go
            # blind just because this strategy happens to be sidelined.
            try:
                state = ta_elliott.compute_state(sym, pack["c1"], pack["c5"],
                                                 pack.get("c15"), params=p)
            except Exception as e:
                self.bus.log(self.name,
                             f"{sym}: state computation FAILED "
                             f"({type(e).__name__}: {e})")
                continue
            self.bus.set(f"ta_state:{sym}", state)
            if not state.get("ok"):
                skipped["state_not_ok"] += 1
                continue
            phases[sym] = state.get("phase")

            # CONFLUENCE IS EVALUATED AND LOGGED ON EVERY CYCLE, before
            # any tradeability gate. Until v58.32 this ran only INSIDE
            # the auto_deploy gate, so running with auto_deploy off —
            # which is how the strategy ships, and the only sane way to
            # observe a new one — captured nothing whatsoever. The bus
            # key it did set was in-memory and overwritten each cycle,
            # so a full session left no record to calibrate against.
            # Calibration is the explicit open question for S9 (replay
            # showed only 1 of 7 signals firing on synthetic data), and
            # it cannot be answered without this.
            key = f"{sym}:ta_elliott"
            pivots = structure.zigzag_series(
                pack["c1"], cfg.get("ta_zigzag_deviation_pct", 0.5))
            try:
                ev, conf = ta_elliott.evaluate(
                    state, pack["c1"], params=p,
                    taken_today=self._taken.get(key, 0), pivots=pivots)
            except Exception as e:
                self.bus.log(self.name,
                             f"{sym}: evaluate FAILED ({type(e).__name__}: {e})")
                continue
            self.bus.set(f"ta_confluence:{sym}", conf)
            _live = self.bus.get("ta_confluence_live") or {}
            _live[sym] = conf.get("count", "?")
            self.bus.set("ta_confluence_live", _live)
            if cfg.get("ta_calibration_logging", True):
                try:
                    import history as _hist
                    _hist.log_ta_observation(
                        sym, "ta_elliott", state, conf, fired=bool(ev),
                        blocked=next((str(v) for v in conf.values()
                                      if isinstance(v, str) and "blocked" in v),
                                     None))
                except Exception as e:
                    # Never let a logging failure stop the strategy.
                    self.bus.log(self.name,
                                 f"{sym}: calibration log failed "
                                 f"({type(e).__name__}: {e})")
            if ev:
                observed.append(f"{sym}:{conf.get('count', '?')}")

            if not cfg.get("ta_auto_deploy", False):
                continue      # observed and logged above; just cannot trade
            if not cfg.get("paper_mode", True):
                continue      # paper-only on introduction
            if sym in positions:
                skipped["position_open"] += 1
                continue
            if time.time() - self._cool.get(key, 0) < 1800:
                skipped["on_cooldown"] += 1
                continue
            analysis = self.bus.get(f"analysis:{sym}")
            if not analysis:
                skipped["no_analysis"] += 1
                continue
            if not ev:
                skipped["no_setup"] += 1
                continue
            leg = "ce" if ev["dir"] > 0 else "pe"
            row = next((r for r in analysis.get("strikes", [])
                        if r["strike"] == analysis.get("atm")), None)
            entry = row and row[leg].get("ltp")
            if not entry:
                continue
            # Structural spot risk -> premium risk via the same
            # 0.5-delta approximation Strategy 7 and 8 already use,
            # clamped 5-30% so a degenerate stop distance can't produce
            # a nonsense premium stop.
            spot_risk = abs(ev["entry_spot"] - ev["stop_spot"]) / max(ev["entry_spot"], 1e-9)
            risk_pct = min(0.30, max(0.05,
                                     spot_risk * ev["entry_spot"] * 0.5 / max(entry, 1e-9)))
            rr = p.get("rr_target", 2.0)
            sig = {"signal": "BUY_CE" if ev["dir"] > 0 else "BUY_PE",
                   "strike": analysis["atm"], "entry": entry,
                   "stoploss": round(entry * (1 - risk_pct), 2),
                   "target1": round(entry * (1 + risk_pct * rr), 2),
                   "target2": round(entry * (1 + risk_pct * rr * 1.33), 2),
                   "spot_invalidation": round(ev["stop_spot"], 1),
                   "confidence": 74, "timeframe": "intraday",
                   "security_id": row[leg].get("security_id"),
                   "reasons": [f"[S9/ta_elliott] {ev['why']}"],
                   "source": "ta_elliott", "setup": "ta_elliott",
                   "ta_confluence": conf}
            self._taken[key] = self._taken.get(key, 0) + 1
            self._cool[key] = time.time()
            fired.append(f"{sym} {sig['signal']}")
            self.bus.log(self.name, f"{sym}: ta_elliott -> {sig['signal']} ({ev['why']})")
            self.bus.publish("signal", {"symbol": sym, "signal": sig,
                                        "analysis": analysis})
        if fired:
            self.summary = " · ".join(fired)
        else:
            ph = ", ".join(f"{s}:{v}" for s, v in phases.items()) or "no state"
            # Confluence counts are surfaced in the summary too, not
            # only in the calibration table — the Agents page is where
            # someone watching live will actually look first.
            live = self.bus.get("ta_confluence_live") or {}
            cc = ", ".join(f"{s}:{c}" for s, c in sorted(live.items())) or "-"
            self.summary = f"phases[{ph}] confluence[{cc}] ({skipped})"
        # 2026-08-03 — WHY THIS IS LOGGED AND NOT ONLY SUMMARISED.
        # `ta_calibration` captured 9 rows on 2026-08-03 against a design
        # of roughly one row per symbol per 5m candle (~300). Diagnosing
        # that offline established what it was NOT — not a candle drought
        # (166k candles that day), not REST starvation (11 rate-limit
        # lines against 98 on a healthy day), not restarts, and not
        # compute_state, which returns ok on 55 of 55 progressive slices
        # of that session. The one thing that could not be checked was
        # the skip profile, because these counters existed ONLY in
        # `self.summary`: an in-memory string on the Agents page that
        # every restart erased. A whole session of evidence was
        # discarded each time the process stopped.
        #
        # Logged on CHANGE rather than every cycle: at 180s a per-cycle
        # line would add ~125 entries a session and be ignored, while the
        # transitions are the whole signal — "no_pack 4" persisting for
        # an hour is the finding.
        _skip_now = tuple(sorted((k, v) for k, v in skipped.items() if v))
        _prev = getattr(self, "_last_skip_profile", None)
        if _skip_now != _prev:
            self._last_skip_profile = _skip_now
            if _skip_now:
                _msg = "skipping: " + ", ".join(f"{k}={v}" for k, v in _skip_now)
                if skipped.get("pack_stale"):
                    # The number that would fix it: how far past the 240s
                    # window the worst pack was.
                    _msg += f" (oldest pack {worst_age:.0f}s, window 240s)"
                self.bus.log(self.name, _msg)
            elif _prev:
                # 2026-08-04 — the first version logged only the skipping
                # state, so SILENCE meant both "still stuck on the same
                # profile" and "recovered". That is the exact ambiguity
                # this session spent a day removing from the futures
                # archive announce, reintroduced by me in the instrument
                # built to diagnose it. Caught live: at 09:15 the profile
                # was no_pack=4 and at 09:27 state_not_ok=4, and only
                # because it CHANGED rather than cleared did the log stay
                # informative. Recovery now says so, once.
                self.bus.log(self.name, "evaluating normally again "
                                        "(no symbols skipped)")


AGENT_CLASSES = [MarketDataAgent, TechnicalAgent, RegimeAgent, NewsAgent,
                 SocialAgent, FundamentalAgent, StrategyAgent, RiskAgent,
                 ExecutionAgent, LearningAgent, BacktestAgent, PriceActionAgent,
                 MTFConfluenceAgent, TAElliottAgent]
if NewsMacroAgent is not None:
    AGENT_CLASSES.append(NewsMacroAgent)
if MarketSenseAgent is not None:
    AGENT_CLASSES.append(MarketSenseAgent)
if TelegramAgent is not None:
    AGENT_CLASSES.append(TelegramAgent)


class Orchestrator:
    def __init__(self, get_chain, orders_factory):
        self.bus = Bus()
        self.ctx = {"get_chain": get_chain, "orders_factory": orders_factory}
        self.agents = []
        self.running = False
        # Restore historical trades so P&L view survives restarts/updates
        history = load_persisted_trades()
        # v59.71 — same window cap the per-close append uses; the full
        # record stays in trades.jsonl.
        _cap = int(config.load().get("closed_trades_memory_cap", 5000) or 5000)
        self.bus.set("closed_trades", history[-_cap:])
        # 2026-07-28 — one-time startup migration cleaning up duplicate
        # journal entries from before the journal_done-vs-restart fix
        # (see _dedupe_journal_file's own docstring for the root cause).
        # No-op on every run after the first, once files are clean.
        for path, key in ((JOURNAL, "date"), (WEEKLY_RISK_JOURNAL, "week")):
            _dedupe_journal_file(path, key,
                                log=lambda m: self.bus.log("orchestrator", m))
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
        # Restore open positions/spreads so a restart (e.g. to apply an
        # update) doesn't silently lose track of anything currently open.
        # Data (premium, spot, etc.) captured at save time may now be
        # stale — agents re-fetch live prices on their next cycle as
        # normal, this just re-seeds WHICH positions exist to manage.
        open_positions, open_spreads = load_open_state()
        # v59.72 (R2 findings L6/L7) — restored entries get a fresh
        # pnl_ts (a pre-v59.70 snapshot has none, which read as "stale
        # for years" to the kill-switch on the first cycle) and any
        # exit_attempt_ts is dropped (a restart within the retry
        # cooldown of a failed SELL must not stay exit-blocked).
        for _p in list((open_positions or {}).values())                 + list((open_spreads or {}).values()):
            if isinstance(_p, dict):
                _p.setdefault("pnl_ts", time.time())
                _p.pop("exit_attempt_ts", None)
        if open_positions:
            self.bus.set("positions", open_positions)
        if open_spreads:
            self.bus.set("spreads", open_spreads)
        if open_positions or open_spreads:
            self.bus.log("orchestrator",
                         f"restored {len(open_positions)} open position(s) and "
                         f"{len(open_spreads)} open spread(s) from before restart "
                         f"— re-validating live prices on next cycle")
        # v59.68 (third-eye Tier 0) — lot-size reconciliation AT STARTUP,
        # not only in LearningAgent's daily-maintenance slot, where it was
        # nested inside the chain-prune guard: skipped whenever the prune
        # had already run that day, skipped when the prune threw first,
        # and its report went to a log line nobody is required to read.
        # A stale lot size rescales every P&L figure by exactly its ratio
        # (the 2026-08-01 NIFTY 75→65 / FINNIFTY 65→60 drift did), so a
        # mismatch is a HIGH alert on every boot, before any agent trades.
        # Still report-only by design — writing config would silently
        # rescale open positions' notional (see reconcile_lot_sizes).
        try:
            import futures_costs as _fc
            _mismatches = _fc.reconcile_lot_sizes()
            for m in _mismatches:
                if m.get("scrip") is None:
                    self.bus.alert("high", "orchestrator", m.get("symbol", "?"),
                                   f"LOT SIZE UNVERIFIABLE at startup: config "
                                   f"says {m.get('config')} but the scrip "
                                   f"master could not answer ({m.get('error')})")
                    continue
                self.bus.alert("high", "orchestrator", m.get("symbol", "?"),
                               f"LOT SIZE MISMATCH at startup: config says "
                               f"{m.get('config')}, scrip master says "
                               f"{m.get('scrip')} ({m.get('pct')}% off) — "
                               f"every rupee figure for this symbol is "
                               f"scaled by that ratio until fixed in Settings")
            if _mismatches:
                self.bus.set("lot_size_mismatches", _mismatches)
            else:
                self.bus.log("orchestrator",
                             "lot sizes reconciled against scrip master — clean")
        except Exception as e:
            # Unavailable scrip master must not block startup, but it is
            # still said out loud — an unverifiable contract size is a
            # measurement risk, not a routine condition.
            self.bus.log("orchestrator",
                         f"⚠ lot-size reconciliation unavailable at startup "
                         f"({type(e).__name__}: {e}) — contract sizes UNVERIFIED")

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

    def exit_position(self, reason="manual exit from dashboard", symbol=None):
        ex = next((a for a in self.agents if a.name == "execution"), None)
        if ex:
            return {**(ex.exit(reason, symbol=symbol) or {}), **self.status()}
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
        positions = self.bus.get("positions", {}) or {}
        cfg = config.load()
        if sym in positions:
            return {"error": f"Already have an open position on {sym}."}
        if len(positions) >= cfg.get("max_concurrent_positions", 1):
            return {"error": f"Max concurrent positions "
                             f"({cfg.get('max_concurrent_positions', 1)}) reached."}
        analysis = self.bus.get(f"analysis:{sym}")
        # Bug found 2026-07-22: two issues compounded into a confusing
        # "regime doesn't allow this" rejection for what looked like a
        # plain WAIT signal on screen.
        #   1) last_signal was a single GLOBAL bus key shared across
        #      every symbol, not namespaced like analysis:{sym} already
        #      is — checking one symbol's signal then confirming a
        #      DIFFERENT symbol could silently combine the wrong
        #      symbol's stale signal with the current symbol's analysis.
        #      Now reads the already-namespaced signal_cache:{sym}.
        #   2) the WAIT guard used a denylist (`sig["signal"] == "WAIT"`)
        #      instead of an allowlist. Any unexpected/malformed signal
        #      value that wasn't literally "WAIT" slipped straight past
        #      this guard into the risk agent, which then correctly
        #      rejected it for not being a real direction — but the UI's
        #      own label rendering defaults anything non-CE/PE to
        #      DISPLAY as "WAIT" too, so the user only ever saw what
        #      looked like a harmless WAIT card with an inexplicable
        #      rejection behind it.
        sig = self.bus.get(f"signal_cache:{sym}")
        if not (analysis and sig) or sig.get("signal") not in ("BUY_CE", "BUY_PE"):
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

    def webhook_signal(self, symbol, direction, strategy_name="tradingview",
                       atr=None, confidence=70):
        """Turn a TradingView webhook alert into an actual option trade.

        TradingView's Pine Script strategies compute everything in
        INDEX POINTS (its own candle engine, its own indicators) — it
        has no concept of option strikes/premiums/security_ids. This
        method does the SAME translation MTFConfluenceAgent already
        does: pick the current ATM strike from the live chain, size
        the stop/target in premium terms (ATR-scaled by delta=0.5 for
        an ATM option if a Pine-computed ATR was sent, else a sane
        fixed-% fallback), then route through the IDENTICAL risk
        pipeline every other signal source uses — no special exemption
        for a webhook-sourced signal, same as every strategy in this
        codebase.

        Not called directly from an agent thread — this runs on the
        FastAPI request thread when the webhook POST arrives, so it
        must be safe to call anytime (same expectation as
        MarketDataAgent._on_ws_tick, which also runs off its own
        thread)."""
        if not self.running:
            return {"error": "Agents aren't running — start them first."}
        sym = symbol.upper()
        direction = direction.lower()
        if direction not in ("bullish", "bearish", "buy", "sell", "long", "short"):
            return {"error": f"Unrecognized direction {direction!r} — expected "
                             f"bullish/bearish (or buy/sell, long/short)"}
        bullish = direction in ("bullish", "buy", "long")
        positions = self.bus.get("positions", {}) or {}
        if sym in positions:
            return {"error": f"Already have an open position on {sym} — "
                             f"webhook signal not acted on"}
        analysis = self.bus.get(f"analysis:{sym}")
        chain = self.bus.get(f"chain:{sym}")
        if not chain or not chain.get("rows") or not chain.get("spot"):
            return {"error": f"No live chain data for {sym} yet"}
        atm = min(chain["rows"], key=lambda r: abs(r["strike"] - chain["spot"]))
        leg = "ce" if bullish else "pe"
        entry = atm[leg].get("ltp")
        if not entry:
            return {"error": f"No ATM {leg.upper()} price available for {sym}"}
        # Same ATR-scaled premium-stop approach as MTFConfluenceAgent,
        # including the same sanity clamp (a bug found there earlier:
        # an unclamped ATR-scaled distance can exceed the ENTIRE
        # premium when ATR is large relative to that day's IV).
        sl_pts_index = 1.5 * atr if atr else entry * 0.30
        # 2026-08-02 — routed through analyzer.option_stop_geometry(), the
        # single definition. This site used its OWN clamp ([10%, 60%])
        # while two other paths used [5%, 30%] and a third used a flat
        # 30%, which is what produced discrete stop widths in the journal.
        from analyzer import option_stop_geometry as _osg
        _atr_pct = (100.0 * atr / analysis["spot"]) if (atr and analysis.get("spot")) else None
        # cfg=None so the helper loads it: `webhook_signal` (the other
        # call site) has no `cfg` in scope, and passing one there would
        # NameError at runtime while compiling cleanly.
        stoploss, _t1, _t2, _sg_meta = _osg(entry, None, atr_pct=_atr_pct,
                                            spot=analysis.get("spot"))
        sl_pts_premium = entry - stoploss
        risk = entry - stoploss
        if risk <= 0:
            return {"error": "Degenerate stop distance — refusing to trade"}
        sig = {
            "signal": "BUY_CE" if bullish else "BUY_PE",
            "strike": atm["strike"], "entry": entry, "stoploss": stoploss,
            "target1": round(entry + risk * 2, 2),
            "target2": round(entry + risk * 2.67, 2),
            "confidence": confidence, "timeframe": "swing",
            "security_id": atm[leg].get("security_id"),
            "reasons": [f"[tradingview:{strategy_name}] webhook alert, "
                       f"direction={direction}" + (f", atr={atr}" if atr else "")],
            "source": f"tradingview_{strategy_name}",
            "atr": atr,
        }
        job = {"symbol": sym, "signal": sig, "analysis": analysis or {}}
        risk_agent = next((a for a in self.agents if a.name == "risk"), None)
        ex = next((a for a in self.agents if a.name == "execution"), None)
        ok, checks = risk_agent.evaluate(job)
        self.bus.log("risk", f"TradingView webhook ({strategy_name}) {sym} "
                            f"{'APPROVED' if ok else 'REJECTED'} — " +
                            " · ".join(checks))
        if not ok:
            return {"error": "Risk agent rejected the webhook signal",
                    "checks": checks}
        ex.place(job, manual=False)
        return {"ok": True, "symbol": sym, "signal": sig["signal"],
                "strike": sig["strike"], "entry": entry}

    def status(self):
        cfg = config.load()
        import sizing
        positions = self.bus.get("positions", {}) or {}
        spreads = self.bus.get("spreads", {}) or {}
        total_capital = cfg.get("backtest_capital", 200000)
        capital_used = sizing.deployed_capital(cfg, positions, spreads)
        today = now_ist().strftime("%Y-%m-%d")
        closed_today = [t for t in self.bus.get("closed_trades", [])
                        if str(t.get("closed_date", "")) == today
                        or str(t.get("opened", "")).startswith(today)]
        realized_today = sum(t.get("pnl", 0) for t in closed_today)
        unrealized_today = (sum(p.get("pnl", 0) for p in positions.values())
                            + sum(sp.get("pnl", 0) for sp in spreads.values()))
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
            "positions": positions,
            "spreads": spreads,
            "max_concurrent_positions": cfg.get("max_concurrent_positions", 1),
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
            # 2026-07-26 — falls back to the last-session read so the
            # Regime panel shows a real classification of the previous
            # session instead of "waiting for enough candles" all
            # evening and weekend. This is a DISPLAY path only: the
            # stale payload carries session_date + stale=True and the
            # dashboard labels it as such, while trade logic keeps
            # reading the plain `regime:{sym}` key, which never holds a
            # stale read (see RegimeAgent.cycle).
            "regime": (self.bus.get(f"regime:{self.bus.get('active_symbol')}")
                       or self.bus.get(
                           f"regime_last_session:{self.bus.get('active_symbol')}")),
            "regimes": {s: (self.bus.get(f"regime:{s}")
                            or self.bus.get(f"regime_last_session:{s}"))
                        for s in self.bus.get("symbols", [])
                        if (self.bus.get(f"regime:{s}")
                            or self.bus.get(f"regime_last_session:{s}"))},
            "alerts": list(self.bus.alerts)[-30:][::-1],
            "log": list(self.bus.feed)[-50:],
            "capital": {
                "total": total_capital,
                "used": round(capital_used, 0),
                "remaining": round(max(0.0, total_capital - capital_used), 0),
                "day_pnl": round(realized_today + unrealized_today, 0),
                "day_pnl_realized": round(realized_today, 0),
                "day_pnl_unrealized": round(unrealized_today, 0),
            },
        }
