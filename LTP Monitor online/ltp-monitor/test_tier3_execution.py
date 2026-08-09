#!/usr/bin/env python3
"""test_tier3_execution.py — v59.69, third-eye Tier 3 (execution realism).

Executable checks for: the shared daily-P&L definition, the futures
daily-loss gate, market-closed-first exit decisions, intrabar backtest
exits, next-bar-open entry fills, size-aware slippage, and the broker
reconciler. Direct-method calls with fake buses where an agent's full
construction would drag in threads.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_tier3_execution")

import agents
import backtester as bt
import config

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


class FakeBus:
    def __init__(self, state=None):
        self.state = dict(state or {})
        self.alerts, self.logs = [], []
    def get(self, k, d=None):
        return self.state.get(k, d)
    def set(self, k, v):
        self.state[k] = v
    def log(self, name, msg):
        self.logs.append(msg)
    def alert(self, sev, src, sym, msg):
        self.alerts.append((sev, msg))


TODAY = agents.now_ist().strftime("%Y-%m-%d")

# --- realized_pnl_today: one definition, date-scoped, restart-proof -----
bus = FakeBus({"closed_trades": [
    {"pnl": -4000, "closed_date": TODAY},
    {"pnl": 1000, "closed_date": TODAY},
    {"pnl": -99999, "closed_date": "2026-01-01"},        # not today
    {"pnl": -500, "closed_at": f"{TODAY}T10:00:00+05:30"},  # fallback field
]})
check("realized_pnl_today sums only today's trades",
      agents.realized_pnl_today(bus) == -3500,
      f"got {agents.realized_pnl_today(bus)}")
check("empty book means zero, not None", agents.realized_pnl_today(FakeBus()) == 0)
# Restart-proofness is structural: the figure derives from closed_trades,
# which the orchestrator reloads from trades.jsonl at boot — there is no
# incremental counter left to zero.

# --- the futures daily-loss gate ---------------------------------------
ex = object.__new__(agents.ExecutionAgent)
ex.name = "execution"
ex.ctx = {}
_orig_open = agents.market_open
_orig_load = config.load
_base_cfg = config.load()
try:
    agents.market_open = lambda: True
    config.load = lambda: {**_base_cfg, "paper_mode": True,
                           "daily_loss_limit": 5000,
                           "futures_risk_per_trade_rupees": 2500,
                           "paused_symbols": []}
    ex.bus = FakeBus({"closed_trades": [{"pnl": -4000, "closed_date": TODAY}]})
    r = ex.enter_future("NIFTY", "LONG", lots=1)
    check("futures entry BLOCKED when the day + risk would breach the limit",
          "daily loss limit" in (r.get("error") or ""), str(r))
    ex.bus = FakeBus({"closed_trades": [{"pnl": -1000, "closed_date": TODAY}]})
    r2 = ex.enter_future("NIFTY", "LONG", lots=1)
    check("futures entry passes the gate when headroom remains "
          "(fails later on missing feed, proving the gate was traversed)",
          "no live futures price" in (r2.get("error") or ""), str(r2))
finally:
    agents.market_open = _orig_open
    config.load = _orig_load

# --- market-closed pre-empts the whole spread exit chain ---------------
cfg = config.load()
reason = agents.spread_exit_reason({}, 999.0, None, cfg, 0, False)
check("closed market returns square-off BEFORE touching any field "
      "(an empty spread dict would KeyError in every other branch)",
      reason == "market closing — squaring off spread", str(reason))
sp = {"legs": [{"leg": "PE", "entry": 100.0, "action": "SELL"}],
      "short_strike": 24000, "width": 100, "loss_limit": 30.0,
      "profit_target": 10.0, "credit": 50.0, "qty": 65, "symbol": "NIFTY",
      "strategy": "bull_put_spread", "mfe": 0}
reason2 = agents.spread_exit_reason(dict(sp), 12.0, 24500, cfg,
                                    0, True)
check("open market still takes profit normally",
      reason2 is not None and "captured" in reason2, str(reason2))

# --- _bar_exit: intrabar resolution, conservative ----------------------
def _pos():
    return {"dir": 1, "entry": 105.0, "stop": 100.0, "stop0": 100.0,
            "t1": 110.0, "t2": 120.0, "t1_done": False}

p = _pos()
px, why = bt._bar_exit(p, {"high": 121.0, "low": 99.0, "close": 120.0}, False)
check("a bar spanning BOTH stop and target is charged as the STOP",
      (px, why) == (100.0, "stop"))
p = _pos()
px, why = bt._bar_exit(p, {"high": 99.5, "low": 98.0, "close": 104.0}, False)
check("pierced-and-recovered bar stops out at the LEVEL "
      "(the old close-only test let it survive)",
      (px, why) == (100.0, "stop"))
p = _pos()
px, why = bt._bar_exit(p, {"high": 120.5, "low": 106.0, "close": 118.0}, False)
check("target fills at the target level, not the close",
      (px, why) == (120.0, "target-2"))
p = _pos()
px, why = bt._bar_exit(p, {"high": 111.0, "low": 101.0, "close": 108.0}, False)
check("T1 ratchet does not rescue the SAME bar", (px, why) == (None, None)
      and p["t1_done"] and p["stop"] == 105.0)
px, why = bt._bar_exit(p, {"high": 108.0, "low": 104.0, "close": 107.0}, False)
check("…but binds from the NEXT bar (breakeven stop)",
      (px, why) == (105.0, "stop"))
p = _pos()
px, why = bt._bar_exit(p, {"high": 108.0, "low": 104.0, "close": 106.5}, True)
check("EOD exits at the close", (px, why) == (106.5, "EOD"))

# --- next-bar-open entry fill in replay_pa -----------------------------
CANDLES = []
for i in range(80):        # replay_pa skips days with < 60 candles
    base = 100.0 + i * 0.1
    CANDLES.append({"ts": 1000 + i * 60, "open": round(base, 2),
                    "high": round(base + 0.4, 2), "low": round(base - 0.4, 2),
                    "close": round(base + 0.2, 2), "volume": 1000})
CANDLES[13]["open"] = 101.77          # the fill we expect
CANDLES[20]["low"] = 90.0             # forces the stop later

class _H:
    @staticmethod
    def index_days(sym, n=250):
        return ["2026-08-01"]
    @staticmethod
    def day_index_candles(sym, day, for_compute=False):
        return [dict(c) for c in CANDLES]

def _stub_eval(name, c1, c5, c15, params=None, taken_today=0, precomputed=None):
    if len(c1) == 13 and taken_today == 0:     # signal on bar index 12
        s = c1[-1]["close"]
        return {"dir": 1, "entry_spot": s, "stop_spot": round(s - 3, 2),
                "t1_spot": s + 5, "t2_spot": s + 10}
    return None

_orig_hist, _orig_eval2 = bt.history, bt.pa.evaluate
try:
    bt.history = _H
    bt.pa.evaluate = _stub_eval
    trades = bt.replay_pa("NIFTY", "momentum_confluence",
                          params={"max_trades_per_day": 1})
finally:
    bt.history = _orig_hist
    bt.pa.evaluate = _orig_eval2
check("signal fires once and produces one trade", len(trades) == 1,
      f"{len(trades)} trades")
if trades:
    t = trades[0]
    check("entry fills at the NEXT bar's open, not the signal bar's close",
          t["entry_spot"] == 101.77, f"filled {t['entry_spot']}")
    check("stop exit fills at the stop level",
          t["reason"] == "stop" and t["exit_spot"] == round(101.4 - 3, 2),
          f"{t['reason']} @ {t['exit_spot']}")

# --- size-aware slippage ------------------------------------------------
cfg = config.load()
r1 = agents.realistic_costs("option", "NIFTY", 1, 150.0, 140.0, cfg)
r5 = agents.realistic_costs("option", "NIFTY", 5, 150.0, 140.0, cfg)
check("statutory charges scale linearly with lots",
      abs(r5["fees"] - 5 * r1["fees"]) <= 5)
check("spread cost scales super-linearly (n^1.5 at alpha=0.5)",
      abs(r5["slippage"] / r1["slippage"] - 5 ** 1.5) < 0.2,
      f"ratio {r5['slippage'] / r1['slippage']:.2f} vs {5 ** 1.5:.2f}")
r5_lin = agents.realistic_costs("option", "NIFTY", 5, 150.0, 140.0,
                                {**cfg, "slippage_impact_alpha": 0})
check("alpha=0 restores the linear model",
      abs(r5_lin["slippage"] - 5 * r1["slippage"]) <= 5)

# --- broker reconciler --------------------------------------------------
class _FakeOrders:
    def positions(self):
        return [{"securityId": "111", "netQty": 50}]

rex = object.__new__(agents.ExecutionAgent)
rex.name = "execution"
rex.ctx = {"orders_factory": lambda: _FakeOrders()}
rex.bus = FakeBus({"positions": {"NIFTY": {"security_id": "111", "qty": 75}}})
_orig_load = config.load
try:
    config.load = lambda: {**_base_cfg, "paper_mode": False,
                           "broker_reconcile_interval_sec": 0}
    rex._reconcile_broker()
finally:
    config.load = _orig_load
check("book-vs-broker mismatch raises a HIGH alert",
      any(sev == "high" and "MISMATCH" in msg for sev, msg in rex.bus.alerts),
      str(rex.bus.alerts))
rec = rex.bus.get("broker_reconcile") or {}
check("reconcile result is published on the bus",
      rec.get("mismatches") and rec["mismatches"][0]["ours"] == 75
      and rec["mismatches"][0]["broker"] == 50)
rex2 = object.__new__(agents.ExecutionAgent)
rex2.name = "execution"
rex2.ctx = {"orders_factory": lambda: _FakeOrders()}
rex2.bus = FakeBus()
rex2._reconcile_broker()     # paper mode (default config) → no-op
check("paper mode skips reconciliation entirely",
      not rex2.bus.alerts and rex2.bus.get("broker_reconcile") is None)

# --- config keys registered --------------------------------------------
for k in ("exit_quote_max_age_sec", "broker_reconcile_interval_sec",
          "exit_retry_cooldown_sec", "slippage_impact_alpha"):
    check(f"'{k}' registered in DEFAULTS", k in config.DEFAULTS)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tier-3 execution checks passed")
