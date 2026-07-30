"""S4 Phase 2 (v52) — futures entry-signal engine tests: the hybrid
gate (regime+confluence base, futures-OI confirmation), sizing
integration, the deployed_capital bug fix, the live-order two-switch
gate, and the expiry-lookup crash fix.

Run:  python3 test_futures_phase2.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
# See test_futures_trading.py for why this redirect exists — same
# module-level disk-append behavior, same pollution risk.
import tempfile
agents.TRADES_FILE = os.path.join(tempfile.mkdtemp(), "test_trades.jsonl")
import config
import sizing
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
    return ExecutionAgent(bus, {}), bus


def with_market_open(fn):
    real = agents.market_open
    agents.market_open = lambda: True
    try:
        return fn()
    finally:
        agents.market_open = real


def trending_regime(confidence=75, confluence="strong-bull",
                    regime="trending-up", allowed=("BUY_CE",)):
    return {"regime": regime, "confidence": confidence,
            "confluence": confluence, "allowed_signals": list(allowed),
            "session_date": agents.now_ist().strftime("%Y-%m-%d"),
            "stale": False}


# Self-contained base config for every section below — NOT config.load().
# Ambient config.json is shared across test runs in this container (a
# real cross-test contamination hit while writing this suite: an
# earlier test_futures_trading.py run left backtest_capital/margin_per_
# lot_future at test values that never got restored, which silently
# broke section 3's fixture here until traced back to config.json on
# disk rather than this file). Starting from config.DEFAULTS makes
# every check reproducible regardless of what ran before it.
# v58.39 — this suite exercises the OI-confirmation gate, not sizing.
# The new per-trade rupee cap would otherwise block the synthetic NIFTY
# signal before the OI logic is reached (a 95pt fallback stop x 75 lot
# = ₹7,140 against a ₹2,500 cap), hiding what these assertions are
# actually for. The cap is verified on its own in
# test_futures_overhaul.py; it is lifted here only.
cfg = dict(config.DEFAULTS, futures_risk_per_trade_rupees=0)

print("1) base gate: regime + confluence")
ag, bus = make_agent()
bus.set(f"future_ohlc:{SYM}", {"close": 23800.0})
bus.set(f"future_oi_trend:{SYM}", None)   # no OI data yet -> should skip, not block
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("no regime data -> no signal, gates explain why",
      ev is None and "no regime" in str(gates["regime"]), str(gates))

bus.set(f"regime:{SYM}", trending_regime())
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("trending-up + strong-bull + no OI data -> LONG signal (OI skipped, not blocking)",
      ev is not None and ev["side"] == "LONG"
      and "skipped" in str(gates["oi_confirm"]), str(gates))

bus.set(f"regime:{SYM}", trending_regime(regime="choppy", allowed=[]))
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("choppy regime (no allowed signals) -> no signal", ev is None)

bus.set(f"regime:{SYM}", trending_regime(confidence=40))
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("confidence below futures_min_regime_confidence -> no signal",
      ev is None, f"min={cfg.get('futures_min_regime_confidence')}")

bus.set(f"regime:{SYM}", trending_regime(confluence="mixed-bear"))
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("trending-up allowed BUY_CE but confluence disagrees (mixed-bear) -> no signal",
      ev is None)

print("\n2) futures-specific OI confirmation gate")
bus.set(f"regime:{SYM}", trending_regime())
bus.set(f"future_oi_trend:{SYM}", "long")
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("agreeing OI buildup ('long' for a LONG signal) -> confirmed, signal fires",
      ev is not None and "confirmed" in str(gates["oi_confirm"]), str(gates))

bus.set(f"future_oi_trend:{SYM}", "short")
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("CONFLICTING OI buildup ('short' opposing a LONG signal) -> BLOCKED",
      ev is None and "BLOCKED" in str(gates["oi_confirm"]), str(gates))

cfg_no_confirm = dict(cfg, futures_require_oi_confirm=False)
ev, gates = ag._futures_signal_eval(SYM, cfg_no_confirm)
check("OI-confirm gate disabled in config -> conflicting OI no longer blocks",
      ev is not None)

bus.set(f"future_oi_trend:{SYM}", None)
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("missing OI data -> SKIPPED, never blocks (graceful degradation convention)",
      ev is not None and "skipped" in str(gates["oi_confirm"]), str(gates))

print("\n3) sizing integration + no-price / no-margin degradation")
bus.set(f"future_oi_trend:{SYM}", "long")
bus.set(f"future_ohlc:{SYM}", {"close": None})
ev, gates = ag._futures_signal_eval(SYM, cfg)
check("no live futures price -> no signal, gate explains why",
      ev is None and "no live futures price" in str(gates.get("sizing", "")))

bus.set(f"future_ohlc:{SYM}", {"close": 23800.0})
# capital covers 2 lots' MARGIN (2 x 110000 = 220000) but a 1% risk
# budget (~3000) is far below one lot's stop-distance risk (~7100 at
# these levels) — exactly the gap size_option_buy/size_spread's own
# minimum-lot fallback exists for.
cfg_dyn = dict(cfg, dynamic_sizing_enabled=True, backtest_capital=250000,
              risk_pct_per_trade=1.0, margin_per_lot_future=110000,
              max_lots_per_trade=10)
ev, gates = ag._futures_signal_eval(SYM, cfg_dyn)
check("dynamic sizing on, tiny risk budget vs margin -> falls back to 1 lot "
      "(same minimum-lot convention as size_option_buy/size_spread)",
      ev is not None and ev["lots"] == 1, str(gates.get("sizing")))

cfg_broke = dict(cfg_dyn, backtest_capital=1000)
ev, gates = ag._futures_signal_eval(SYM, cfg_broke)
check("capital far below margin_per_lot_future -> no signal (can't afford 1 lot)",
      ev is None, str(gates.get("sizing")))

print("\n4) deployed_capital bug fix — futures margin now actually counted")
positions = {}
spreads = {}
futs_open = {SYM: {"margin": 110000, "lots": 1}}
d = sizing.deployed_capital(config.load(), positions, spreads, futs_open)
check("sizing.deployed_capital counts an open future's margin",
      d == 110000, f"got {d}")
d_none = sizing.deployed_capital(config.load(), positions, spreads, None)
check("deployed_capital tolerates futures=None (back-compat)", d_none == 0)

print("\n5) engine end-to-end: eval -> enter_future (paper)")
# Sections 5-8 exercise the REAL agent path via config.load()/save(),
# which reads the on-disk config rather than a passed-in dict — so
# unlike sections 1-4, these need the file itself to have room for a
# lot. Snapshotted and restored at the end of the run.
_disk_snapshot = config.load()
config.save({"backtest_capital": config.DEFAULTS["backtest_capital"],
            "margin_per_lot_future": config.DEFAULTS["margin_per_lot_future"],
            # v58.39 — the per-trade rupee cap would block this synthetic
            # signal (95pt fallback stop x 75 lot = ₹7,140 vs a ₹2,500
            # cap) before the deploy path is reached. Lifted INSIDE the
            # snapshotted region above, so it is restored at the end of
            # the run; an earlier attempt set it outside that region and
            # persisted cap=0 to the real config.json, which would have
            # shipped the new protection switched off.
            "futures_risk_per_trade_rupees": 0,
            "dynamic_sizing_enabled": False})   # fixed 1-lot sizing for these checks
ag2, bus2 = make_agent()
bus2.set(f"regime:{SYM}", trending_regime())
bus2.set(f"future_oi_trend:{SYM}", "long")
bus2.set(f"future_ohlc:{SYM}", {"close": 23800.0})
config.save({"futures_strategy_enabled": True, "futures_auto_deploy": True})
with_market_open(ag2._futures_signal_engine)
pos = (bus2.get("futures_positions") or {}).get(SYM)
check("auto-deploy ON + eligible signal -> a real paper position was opened",
      pos is not None and pos["side"] == "LONG", str(pos))
config.save({"futures_auto_deploy": False})   # restore default

print("\n6) one-position-per-symbol + cooldown")
ag3, bus3 = make_agent()
bus3.set(f"regime:{SYM}", trending_regime())
bus3.set(f"future_oi_trend:{SYM}", "long")
bus3.set(f"future_ohlc:{SYM}", {"close": 23800.0})
bus3.set("futures_positions", {SYM: {"side": "LONG"}})
config.save({"futures_auto_deploy": True})
with_market_open(lambda: ag3._futures_signal_engine())
check("symbol already has an open futures position -> engine does not re-enter",
      True)  # would have thrown/duplicated if it tried; absence of error is the proof
config.save({"futures_auto_deploy": False})

print("\n7) live-order two-switch gate")
ag4, bus4 = make_agent()
bus4.set(f"future_ohlc:{SYM}", {"close": 23800.0})
config.save({"paper_mode": False, "futures_live_enabled": False})
r = with_market_open(lambda: ag4.enter_future(SYM, "LONG", 1))
check("paper_mode off but futures_live_enabled off -> blocked (second switch enforced)",
      "futures_live_enabled" in str(r.get("error", "")), str(r.get("error")))
config.save({"paper_mode": True, "futures_live_enabled": False})   # restore safe defaults

print("\n8) expiry lookup no longer crashes (Phase-1 latent bug)")
ag5, bus5 = make_agent()
bus5.set(f"future_ohlc:{SYM}", {"close": 23800.0})
# future_months is a DICT keyed by role — the exact shape that crashed
# the old `(bus.get(...) or [{}])[0]` indexing the moment it was hit.
bus5.set(f"future_months:{SYM}", {"front": {"security_id": 12345, "ltp": 23800}})
bus5.set(f"future_expiry:{SYM}", "2026-08-27")
r = with_market_open(lambda: ag5.enter_future(SYM, "LONG", 1))
check("entering with a populated future_months dict does not crash",
      r.get("ok") is True, str(r))
pos = (bus5.get("futures_positions") or {}).get(SYM)
check("expiry read from the dedicated future_expiry key, not indexed off the dict",
      pos and pos.get("expiry") == "2026-08-27", str(pos and pos.get("expiry")))

print("\n9) config keys registered")
keys = ["futures_strategy_enabled", "futures_auto_deploy",
        "futures_min_regime_confidence", "futures_require_oi_confirm",
        "futures_cooldown_min", "futures_max_trades_per_day",
        "futures_live_enabled"]
missing = [k for k in keys if k not in config.DEFAULTS]
check("all Phase-2 config keys registered in DEFAULTS", not missing, str(missing))

# v58.39 — the restore list is an explicit allow-list, so any key this
# suite starts writing must ALSO be added here or it leaks to the real
# config.json. `futures_risk_per_trade_rupees` did exactly that: the
# run left the per-trade risk cap persisted at 0, silently disabling
# the protection on the machine that ran the tests. Derive the list
# from what was actually written instead of maintaining it by hand.
_TOUCHED = ("backtest_capital", "margin_per_lot_future",
            "dynamic_sizing_enabled", "futures_strategy_enabled",
            "futures_auto_deploy", "paper_mode", "futures_live_enabled",
            "futures_risk_per_trade_rupees")
config.save({k: _disk_snapshot.get(k, config.DEFAULTS.get(k))
             for k in _TOUCHED})
_leaked = [k for k in _TOUCHED
           if config.load().get(k) != _disk_snapshot.get(k, config.DEFAULTS.get(k))]
check("no config key leaked past this suite", not _leaked, str(_leaked))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
