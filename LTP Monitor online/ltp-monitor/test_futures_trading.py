"""S4 (v50) — futures paper-trading tests: entry gates, direction-aware
P&L / SL / target / trailing, fee-adjusted close, EOD square-off, and
portfolio kill-switch inclusion.

Run:  python3 test_futures_trading.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
# 2026-07-26 (v53 hygiene) — _append_trade() writes to agents.TRADES_FILE
# unconditionally, regardless of which Bus instance a test uses (it's a
# module-level disk append, not scoped to the in-memory Bus). Every run
# of this suite was leaving real "kind":"future" rows in the actual
# persistent ~/.ltp-monitor/trades.jsonl — harmless to the shipped zip
# (packaging excludes *.jsonl) but real container-side pollution that
# had to be manually cleaned after multiple sessions. Redirect to a
# throwaway path for the duration of this run instead.
import tempfile
agents.TRADES_FILE = os.path.join(tempfile.mkdtemp(), "test_trades.jsonl")
import config
from agents import ExecutionAgent, Bus

SYM = "NIFTY"
results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def make_agent():
    bus = Bus()
    bus.set("symbols", [SYM])
    ag = ExecutionAgent(bus, {})
    return ag, bus


def with_market_open(fn, hour=11, minute=0):
    """Pin market_open AND the wall clock to mid-session. Without the
    clock pin, running the suite after 15:15 real time makes the EOD
    square-off fire inside the same monitor call, closing positions
    before the mid-trade assertions can inspect them (observed live:
    two spurious failures at 18:33 IST while the P&L on the resulting
    close record was exactly correct)."""
    real_mo = agents.market_open
    real_now = agents.now_ist
    agents.market_open = lambda: True
    agents.now_ist = lambda: real_now().replace(hour=hour, minute=minute)
    try:
        return fn()
    finally:
        agents.market_open = real_mo
        agents.now_ist = real_now


cfg = config.load()
cfg_backup = dict(cfg)
LOT = cfg["lot_sizes"].get(SYM, 75)

print("1) entry gating")
ag, bus = make_agent()
r = with_market_open(lambda: ag.enter_future(SYM, "LONG", 1))
check("no futures price -> blocked with a real reason",
      "no live futures price" in str(r.get("error", "")), str(r.get("error"))[:60])
bus.set(f"future_ohlc:{SYM}", {"close": 23800.0, "open": 23750.0})
real_mo = agents.market_open
agents.market_open = lambda: False
r = ag.enter_future(SYM, "LONG", 1)
agents.market_open = real_mo
check("market closed -> blocked", "closed" in str(r.get("error", "")))
r = with_market_open(lambda: ag.enter_future(SYM, "SIDEWAYS", 1))
check("invalid side rejected", "LONG or SHORT" in str(r.get("error", "")))
r = with_market_open(lambda: ag.enter_future(SYM, "LONG", 99))
check("margin gate blocks oversize entry",
      "insufficient margin" in str(r.get("error", "")), str(r.get("error"))[:70])
bus.set("portfolio_halt_until", time.time() + 60)
r = with_market_open(lambda: ag.enter_future(SYM, "LONG", 1))
check("kill-switch cooldown blocks entry", "kill-switch" in str(r.get("error", "")))
bus.set("portfolio_halt_until", 0)

print("\n2) LONG lifecycle: P&L, trailing, target")
# make margin affordable for the test capital, restore at exit
config.save({"margin_per_lot_future": 50000, "backtest_capital": 100000})
import atexit
# Restore to config.DEFAULTS explicitly rather than the snapshot taken
# at the top of this run. A snapshot is only as clean as whatever ran
# before it — if a PRIOR run's own restore ever failed to fire (e.g. the
# process was killed rather than exiting normally), cfg_backup here
# would silently capture and re-save the already-contaminated values,
# compounding the problem invisibly across sessions. DEFAULTS is the
# one value that's never contaminated.
atexit.register(lambda: config.save(
    {"margin_per_lot_future": config.DEFAULTS["margin_per_lot_future"],
     "backtest_capital": config.DEFAULTS["backtest_capital"]}))
r = with_market_open(lambda: ag.enter_future(SYM, "LONG", 1))
check("LONG entry accepted", r.get("ok") is True, str(r.get("error"))[:70])
p = (bus.get("futures_positions") or {}).get(SYM, {})
check("SL below entry, target above (direction-aware)",
      p and p["sl"] < p["entry"] < p["target"],
      f"sl={p.get('sl')} entry={p.get('entry')} tgt={p.get('target')}")
# favourable move: trailing should ratchet the SL up
bus.set(f"future_ohlc:{SYM}", {"close": 23900.0})
with_market_open(ag._monitor_futures)
p = (bus.get("futures_positions") or {}).get(SYM, {})
expected_pnl = (23900 - 23800) * LOT
check("LONG unrealized P&L correct", p and abs(p["pnl"] - expected_pnl) < 1,
      f"pnl={p.get('pnl')} expected={expected_pnl}")
orig_sl = 23800 * (1 - config.load().get("futures_sl_pct", 0.4) / 100)
check("trailing raised the stop above the original",
      p and p["sl"] > orig_sl, f"sl={p.get('sl')} orig={orig_sl:.1f}")
# target hit -> close with fees
bus.set(f"future_ohlc:{SYM}", {"close": 23992.0})
with_market_open(ag._monitor_futures)
check("target exit fired", SYM not in (bus.get("futures_positions") or {}))
closed = (bus.get("closed_trades") or [])[-1]
check("closed record is fee-adjusted NET (gross - ₹80)",
      closed and abs(closed["pnl"] - (closed["gross_pnl"] - 80)) < 1,
      f"gross={closed.get('gross_pnl')} net={closed.get('pnl')} fees={closed.get('fees')}")
check("record tagged kind=future", closed.get("kind") == "future")

print("\n3) SHORT lifecycle: direction-aware stop")
r = with_market_open(lambda: ag.enter_future(SYM, "SHORT", 1))
check("SHORT entry accepted", r.get("ok") is True, str(r.get("error"))[:60])
p = (bus.get("futures_positions") or {}).get(SYM, {})
check("SHORT: SL above entry, target below",
      p and p["target"] < p["entry"] < p["sl"],
      f"tgt={p.get('target')} entry={p.get('entry')} sl={p.get('sl')}")
# adverse move for a short = price UP through the stop
bus.set(f"future_ohlc:{SYM}", {"close": p["sl"] + 5 if p else 0})
with_market_open(ag._monitor_futures)
check("SHORT stoploss fired on upward move",
      SYM not in (bus.get("futures_positions") or {}))
closed = (bus.get("closed_trades") or [])[-1]
check("SHORT loss recorded as negative", closed and closed["gross_pnl"] < 0,
      f"gross={closed.get('gross_pnl')}")

print("\n4) kill-switch includes futures unrealized P&L")
ag2, bus2 = make_agent()
bus2.set(f"future_ohlc:{SYM}", {"close": 23800.0})
r = with_market_open(lambda: ag2.enter_future(SYM, "LONG", 1))
check("entry for kill-switch test", r.get("ok") is True, str(r.get("error"))[:60])
# crash the future far past the drawdown limit
bus2.set(f"future_ohlc:{SYM}", {"close": 23800.0 - 400})
# compute pnl but suppress per-position stop so the KILL-SWITCH does the close:
p = (bus2.get("futures_positions") or {}).get(SYM)
if p:
    p["sl"] = 0.01           # stop far away — only the kill-switch can act
    sign = 1
    p["pnl"] = (23400.0 - p["entry"]) * sign * p["lot_size"] * p["lots"]
    bus2.set("futures_positions", {SYM: p})
with_market_open(ag2._check_portfolio_kill_switch)
check("kill-switch force-closed the futures position",
      SYM not in (bus2.get("futures_positions") or {}))
check("cooldown engaged", bus2.get("portfolio_halt_until", 0) > time.time())

print("\n5) EOD square-off")
ag3, bus3 = make_agent()
bus3.set(f"future_ohlc:{SYM}", {"close": 23800.0})
r = with_market_open(lambda: ag3.enter_future(SYM, "LONG", 1))
check("entry for EOD test", r.get("ok") is True)
with_market_open(ag3._monitor_futures, hour=15, minute=16)
check("15:16 forces square-off", SYM not in (bus3.get("futures_positions") or {}))
closed = (bus3.get("closed_trades") or [])[-1]
check("EOD reason recorded", "EOD" in str(closed.get("reason", "")))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
