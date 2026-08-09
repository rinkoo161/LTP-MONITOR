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
import time

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

# v59.72 (R2 finding M3) — the FILL BAR's own range must be inspected:
# a fill bar whose low pierces the stop exits on THAT bar at the level.
CANDLES2 = [dict(c) for c in CANDLES]
CANDLES2[13]["low"] = 90.0
class _H2(_H):
    @staticmethod
    def day_index_candles(sym, day, for_compute=False):
        return [dict(c) for c in CANDLES2]
_orig_hist2, _orig_eval3 = bt.history, bt.pa.evaluate
try:
    bt.history = _H2
    bt.pa.evaluate = _stub_eval
    trades2 = bt.replay_pa("NIFTY", "momentum_confluence",
                           params={"max_trades_per_day": 1})
finally:
    bt.history = _orig_hist2
    bt.pa.evaluate = _orig_eval3
check("a stop pierced on the FILL BAR exits on the fill bar at the level",
      len(trades2) == 1 and trades2[0]["reason"] == "stop"
      and trades2[0]["exit_spot"] == round(101.4 - 3, 2)
      and trades2[0]["exit_ts"] == CANDLES2[13]["ts"],
      str(trades2[:1]))

# --- size-aware slippage ------------------------------------------------
cfg = config.load()
r1 = agents.realistic_costs("option", "NIFTY", 1, 150.0, 140.0, cfg)
r5 = agents.realistic_costs("option", "NIFTY", 5, 150.0, 140.0, cfg)
# v59.72 (R2) — brokerage is per ORDER: notional charges scale with
# lots, the ₹20×2 brokerage (+18% GST) is charged once. 5 lots must be
# 5× the statutory MINUS the 4 extra brokerage-with-GST the old linear
# scaling over-charged.
_brk = 20.0 * 2 * 1 * 1.18
check("statutory scales per-order: notional x lots, brokerage once",
      abs(r5["fees"] - (5 * r1["fees"] - 4 * _brk)) <= 6,
      f"{r5['fees']} vs {5 * r1['fees'] - 4 * _brk:.0f}")
_f1 = agents.realistic_costs("future", "NIFTY", 1, 25000.0, 25050.0, cfg)
_f5 = agents.realistic_costs("future", "NIFTY", 5, 25000.0, 25050.0, cfg)
import futures_costs as _fc
_b5 = _fc.breakdown("NIFTY", 25000.0, 25050.0, lots=5, cfg=cfg,
                    lot=(cfg.get("lot_sizes") or {}).get("NIFTY", 75))
check("futures multi-lot statutory matches breakdown(lots=n) exactly",
      abs(_f5["fees"] - round(_b5["statutory_rupees"], 0)) <= 1,
      f"{_f5['fees']} vs {_b5['statutory_rupees']:.0f}")
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

# --- round 2: order confirmation ---------------------------------------
class _StatusOrders:
    def __init__(self, status=None, boom=False):
        self._s, self._boom = status, boom
    def order_status(self, oid):
        if self._boom:
            raise RuntimeError("api down")
        return {"data": {"orderStatus": self._s}}

cex = object.__new__(agents.ExecutionAgent)
cex.name = "execution"
cex.bus = FakeBus()
st = cex._confirm_order(_StatusOrders("REJECTED"), {"orderId": "42"}, "BUY X")
check("a broker-side REJECTED raises a HIGH alert",
      st == "REJECTED" and any(s == "high" and "REJECTED" in m
                               for s, m in cex.bus.alerts))
cex.bus = FakeBus()
st = cex._confirm_order(_StatusOrders("TRADED"), {"orderId": "42"}, "BUY X")
check("a TRADED status is logged, not alerted",
      st == "TRADED" and not cex.bus.alerts and cex.bus.logs)
cex.bus = FakeBus()
st = cex._confirm_order(_StatusOrders(boom=True), {"orderId": "42"}, "BUY X")
check("an unreachable status API says UNVERIFIED and never raises",
      st is None and any("UNVERIFIED" in m for m in cex.bus.logs))
check("no order id means nothing to confirm",
      cex._confirm_order(_StatusOrders("TRADED"), {}, "X") is None)

# v59.72 (R2 finding H3) — Dhan returns a JSON ARRAY; the old .get()
# on a list made the whole feature a silent no-op.
class _ListOrders:
    def order_status(self, oid):
        return [{"orderStatus": "REJECTED"}]
class _DataListOrders:
    def order_status(self, oid):
        return {"data": [{"orderStatus": "TRADED"}]}
cex.bus = FakeBus()
st = cex._confirm_order(_ListOrders(), {"orderId": "42"}, "BUY X")
check("a LIST response is parsed (Dhan's real shape) and REJECTED alerts",
      st == "REJECTED" and any(s == "high" for s, _ in cex.bus.alerts),
      f"status={st}")
cex.bus = FakeBus()
check("a data-wrapped LIST response is parsed too",
      cex._confirm_order(_DataListOrders(), {"orderId": "42"}, "X") == "TRADED")

# v59.72 (R2 finding H4) — two futures open at the close must square
# off exactly ONCE each: the old loop wrote a stale local dict back to
# the bus and resurrected the first-closed position.
class FakeBus2(FakeBus):
    def publish(self, topic, msg):
        self.state.setdefault("_pub", []).append(topic)

def _mk_fut(sym, e):
    return {"symbol": sym, "side": "LONG", "lots": 1, "lot_size": 65,
            "entry": e, "ltp": e, "sl": e - 100, "target": e + 100,
            "peak": e, "pnl": 0.0, "pnl_ts": time.time(), "paper": True,
            "mfe": 0, "mae": 0, "opened": "10:00:00",
            "opened_ts": time.time(), "margin": 0}

fex = object.__new__(agents.ExecutionAgent)
fex.name = "execution"
fex.ctx = {}
fex.bus = FakeBus2({
    "futures_positions": {"NIFTY": _mk_fut("NIFTY", 25000.0),
                          "BANKNIFTY": _mk_fut("BANKNIFTY", 57000.0)},
    "closed_trades": [],
    "future_ohlc:NIFTY": {"close": 25010.0, "ts": time.time()},
    "future_ohlc:BANKNIFTY": {"close": 57010.0, "ts": time.time()}})
_orig_open3 = agents.market_open
try:
    agents.market_open = lambda: False
    agents.ExecutionAgent._monitor_futures(fex)
    agents.ExecutionAgent._monitor_futures(fex)   # 2nd cycle: must no-op
finally:
    agents.market_open = _orig_open3
_closed_syms = [t.get("symbol") for t in fex.bus.get("closed_trades", [])]
check("two futures at the close square off exactly once each",
      sorted(_closed_syms) == ["BANKNIFTY", "NIFTY"],
      f"closed: {_closed_syms}")
check("no position is resurrected after its close",
      not fex.bus.get("futures_positions"),
      str(fex.bus.get("futures_positions")))

# --- round 2: kill-switch says when its inputs are stale ---------------
kex = object.__new__(agents.ExecutionAgent)
kex.name = "execution"
kex.ctx = {}
kex.bus = FakeBus({"positions": {"NIFTY": {"pnl": -100, "pnl_ts": 1.0}},
                   "spreads": {}, "futures_positions": {}})
_orig_load = config.load
_orig_open2 = agents.market_open
try:
    config.load = lambda: {**_base_cfg, "portfolio_max_drawdown": 10 ** 9,
                           "exit_quote_max_age_sec": 90,
                           "portfolio_kill_switch_enabled": True}
    agents.market_open = lambda: True
    kex._check_portfolio_kill_switch()
finally:
    config.load = _orig_load
    agents.market_open = _orig_open2
check("stale kill-switch inputs raise an UNVERIFIED alert",
      any("UNVERIFIED" in m for _, m in kex.bus.alerts), str(kex.bus.alerts))

# --- round 2: open-trade fetch priority covers all three books ---------
import inspect
_md_src = inspect.getsource(agents.MarketDataAgent.cycle)
check("market-data priority counts spreads and futures as open trades",
      "spreads" in _md_src and "futures_positions" in _md_src)

# --- config keys registered --------------------------------------------
for k in ("exit_quote_max_age_sec", "broker_reconcile_interval_sec",
          "exit_retry_cooldown_sec", "slippage_impact_alpha"):
    check(f"'{k}' registered in DEFAULTS", k in config.DEFAULTS)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all tier-3 execution checks passed")
