#!/usr/bin/env python3
"""test_option_churn_loop.py — the SENSEX 78700 CE runaway, 2026-08-06.

Observed live, in paper mode, five times in 38 seconds:

    10:24:05  BUY  40 x SENSEX 78700 CE @ Rs 358.85
    10:24:06  SELL 40 x SENSEX 78700 CE @ Rs 363.00  target-2  +Rs 166
    10:24:13  BUY  ...                  @ Rs 358.85     <- identical
    10:24:13  SELL ...                  @ Rs 363.00  target-2  (same second)
    10:24:21  BUY  ...                  @ Rs 358.85
    10:24:23  SELL ...                  @ Rs 363.40
    ...

THREE independent defects compounded. Each is tested separately here,
because each one ALONE is sufficient to break the loop and any one of
them could be reintroduced without the other two noticing:

  1. `target2` was never validated. analyzer only ever corrected it
     inside the `rr < min_rr` branch, so a signal whose target1 already
     cleared the floor kept whatever it arrived with — including None.
     9 of 21 signals in that morning's shadow journal had target2 null.
     ExecutionAgent copies it onto the position unguarded and the exit
     is `elif ltp >= p["target2"]`.

  2. Entry priced off analysis:{sym} (TechnicalAgent, interval=60) while
     the exit read chain:{sym} (MarketDataAgent, every 3s). 20x cadence
     apart, so a fill could be a minute stale against a 3-second-fresh
     exit check. That is the frozen Rs 358.85: five entries inside one
     analysis window while the live chain moved 363.00 -> 366.05.

  3. No re-entry cooldown on directional options, though spreads,
     futures, broker failures and news re-alerts all had one.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_option_churn_loop")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import agents
import analyzer
import config

print("1) target2 must be a real number ABOVE target1")
# Drive the REAL invariant enforcer. The numbers are the live ones.
for label, t1, t2 in (("target2 is None", 574.2, None),
                      ("target2 below target1", 574.2, 400.0),
                      ("target2 equals target1", 574.2, 574.2),
                      ("target2 below ENTRY — the churn case", 574.2, 358.0)):
    sig = {"signal": "BUY_CE", "strike": 78700.0, "entry": 358.85,
           "stoploss": 251.19, "target1": t1, "target2": t2,
           "confidence": 80}
    out, repairs = analyzer.enforce_signal_invariants(
        sig, {"strikes": []}, cfg={"signal_min_rr": 2.0})
    got = out.get("target2")
    check(f"{label:38} -> repaired",
          isinstance(got, (int, float)) and got > out["target1"],
          f"target1={out['target1']} target2={got}")

# A already-valid signal must NOT be touched — a repair that fires on
# good input is just as wrong as one that never fires.
sig_ok = {"signal": "BUY_CE", "strike": 78700.0, "entry": 358.85,
          "stoploss": 251.19, "target1": 574.2, "target2": 646.3,
          "confidence": 80}
out_ok, _ = analyzer.enforce_signal_invariants(
    sig_ok, {"strikes": []}, cfg={"signal_min_rr": 2.0})
check("a VALID target2 is left alone", out_ok["target2"] == 646.3,
      f"{out_ok['target2']} — a repair that fires on good input is a bug too")

print("\n2) the fill uses the LIVE chain, not the stale analysis pack")


def _chain(ltp):
    return {"symbol": "SENSEX", "spot": 78700.0, "rows": [
        {"strike": 78700.0,
         "ce": {"ltp": ltp, "oi": 1000, "oi_chg": 0, "volume": 10, "iv": 20.0,
                "bid": ltp - 0.5, "ask": ltp + 0.5, "security_id": "9001"},
         "pe": {"ltp": 100.0, "oi": 1000, "oi_chg": 0, "volume": 10, "iv": 20.0,
                "bid": 99.5, "ask": 100.5, "security_id": "9002"}}]}


def _job(strike=78700.0, entry=358.85, option_ltp=358.85):
    return {"symbol": "SENSEX",
            "analysis": {"strikes": [], "spot": 78700.0},
            "signal": {"signal": "BUY_CE", "strike": strike, "entry": entry,
                       "option_ltp": option_ltp, "stoploss": 251.19,
                       "target1": 574.2, "target2": 646.3, "confidence": 80,
                       "security_id": "9001", "source": "test"}}


cfg = config.load()
cfg["paper_mode"] = True
cfg["lot_sizes"] = dict(cfg.get("lot_sizes") or {}, SENSEX=20)
cfg["option_reentry_cooldown_sec"] = 180
# The per-trade rupee cap is a DIFFERENT gate and it refuses this
# geometry (108pt stop x 20 = Rs 2,153 > the Rs 2,000 default). Raise it
# here so the churn behaviour is what is under test — otherwise every
# check below passes for the wrong reason, which is worse than failing.
cfg["option_risk_per_trade_rupees"] = 15000
config.save(cfg)

bus = agents.Bus()
bus.set("symbols", ["SENSEX"])
bus.set("chain:SENSEX", _chain(363.0))          # LIVE price, 3s cadence
ex = agents.ExecutionAgent(bus, {"orders_factory": lambda: None,
                                 "get_chain": lambda s: None})
res = ex.place(_job())                           # signal priced at 358.85
pos = (bus.get("positions") or {}).get("SENSEX")
check("a position was opened", pos is not None, str(res)[:120])
if pos:
    check("it filled at the LIVE 363.0, not the stale 358.85",
          pos["entry"] == 363.0, f"entry={pos['entry']}")
    check("and the position's target2 is above its entry",
          pos["target2"] > pos["entry"],
          f"target2={pos['target2']} entry={pos['entry']} — this is the "
          f"comparison the exit check makes")

print("\n3) re-entry cooldown blocks the loop")
if pos:
    ex.exit("test close", symbol="SENSEX")
    check("exit stamped the re-entry block",
          bool(bus.get("option_reentry_block")),
          str(bus.get("option_reentry_block")))
    again = ex.place(_job())
    check("an immediate re-entry is REFUSED",
          isinstance(again, dict) and "cooldown" in str(again.get("error", "")),
          str(again)[:140])
    check("and no position was opened by it",
          not (bus.get("positions") or {}).get("SENSEX"),
          str(bus.get("positions")))

    # A DIFFERENT strike must remain tradeable — a cooldown that blocks
    # the whole symbol would be a shutdown, not a guard.
    bus.set("chain:SENSEX", {"symbol": "SENSEX", "spot": 78700.0, "rows": [
        {"strike": 78800.0,
         "ce": {"ltp": 300.0, "oi": 1000, "oi_chg": 0, "volume": 10,
                "iv": 20.0, "bid": 299.5, "ask": 300.5,
                "security_id": "9003"},
         "pe": {"ltp": 100.0, "oi": 1000, "oi_chg": 0, "volume": 10,
                "iv": 20.0, "bid": 99.5, "ask": 100.5,
                "security_id": "9004"}}]})
    other = ex.place(_job(strike=78800.0, entry=300.0, option_ltp=300.0))
    check("a DIFFERENT strike is still tradeable",
          not (isinstance(other, dict) and "cooldown" in str(other.get("error", ""))),
          f"{str(other)[:110]} — blocking the whole symbol would be a "
          f"shutdown, not a guard")

    # Manual is exempt: an operator re-entering is a decision, not a loop.
    blk = bus.get("option_reentry_block") or {}
    blk["SENSEX:78700.0:CE"] = time.time()
    bus.set("option_reentry_block", blk)
    bus.set("positions", {})
    bus.set("chain:SENSEX", _chain(363.0))
    man = ex.place(_job(), manual=True)
    check("a MANUAL re-entry is exempt",
          not (isinstance(man, dict) and "cooldown" in str(man.get("error", ""))),
          str(man)[:110])

    # And it EXPIRES — a cooldown that never lifts is a shutdown.
    blk = bus.get("option_reentry_block") or {}
    blk["SENSEX:78700.0:CE"] = time.time() - 10_000
    bus.set("option_reentry_block", blk)
    bus.set("positions", {})
    old = ex.place(_job())
    check("the cooldown EXPIRES",
          not (isinstance(old, dict) and "cooldown" in str(old.get("error", ""))),
          str(old)[:110])
    check("the cooldown length is registered in DEFAULTS",
          "option_reentry_cooldown_sec" in config.DEFAULTS,
          "config.save() silently drops unregistered keys")

print("\n3b) a REFUSAL also stamps the cooldown — the approve/refuse loop")
# 2026-08-06, third occurrence of one pattern in a session: S10 zero-lot
# re-fired every 5s, the SENSEX churn opened/closed 5x in 38s, and the
# risk gate APPROVED four BANKNIFTY/FINNIFTY orders in 19s that the
# per-trade rupee cap then refused. All three: the signal-handled
# bookkeeping only ran on a SUCCESSFUL FILL, so refusal paths re-fired
# forever. Stamping on refusal closes all three with one mechanism.
bus3 = agents.Bus()
bus3.set("symbols", ["SENSEX"])
bus3.set("chain:SENSEX", _chain(363.0))
ex3 = agents.ExecutionAgent(bus3, {"orders_factory": lambda: None,
                                   "get_chain": lambda s: None})
cfg_tight = config.load()
cfg_tight["option_risk_per_trade_rupees"] = 100      # forces a refusal
config.save(cfg_tight)
r1 = ex3.place(_job())
check("the order is refused by the rupee cap",
      isinstance(r1, dict) and "error" in r1, str(r1)[:110])
check("the refusal STAMPED the re-entry block",
      bool(bus3.get("option_reentry_block")),
      "without this the same signal re-approves every ~5s forever")
r2 = ex3.place(_job())
check("an immediate retry is now refused by the COOLDOWN",
      isinstance(r2, dict) and "cooldown" in str(r2.get("error", "")),
      str(r2)[:120])
# A cooldown refusal must NOT refresh its own stamp, or it never expires.
_t0 = (bus3.get("option_reentry_block") or {}).get("SENSEX:78700.0:CE")
ex3.place(_job())
_t1 = (bus3.get("option_reentry_block") or {}).get("SENSEX:78700.0:CE")
check("a cooldown refusal does NOT refresh its own stamp", _t0 == _t1,
      f"{_t0} -> {_t1} — refreshing would make the cooldown permanent")
cfg_tight["option_risk_per_trade_rupees"] = 15000
config.save(cfg_tight)

print("\n4) the whole loop, end to end — the observed scenario")
# Stale signal at 358.85, live chain at 363.0, and a target2 that the
# OLD code would have accepted at 363.0. All three fixes must combine
# so that this cannot open-and-instantly-close.
bus2 = agents.Bus()
bus2.set("symbols", ["SENSEX"])
bus2.set("chain:SENSEX", _chain(363.0))
ex2 = agents.ExecutionAgent(bus2, {"orders_factory": lambda: None,
                                   "get_chain": lambda s: None})
churn_job = _job()
churn_job["signal"]["target2"] = 363.0        # the value that fired
churn_sig, _ = analyzer.enforce_signal_invariants(
    churn_job["signal"], {"strikes": []}, cfg={"signal_min_rr": 2.0})
churn_job["signal"] = churn_sig
ex2.place(churn_job)
p2 = (bus2.get("positions") or {}).get("SENSEX")
check("the churn signal still opens a position", p2 is not None)
if p2:
    check("its target2 is NOT already satisfied by the live price",
          not (p2["ltp"] >= p2["target2"]),
          f"ltp={p2['ltp']} target2={p2['target2']} — `ltp >= target2` is "
          f"the exact exit test; true here means instant exit")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all option churn-loop checks passed")
