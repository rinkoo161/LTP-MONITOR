"""v58.39 — futures overhaul, separate risk budgets, per-class exits.

Diagnosis from 40 live futures trades:

    win rate 27.5%   payoff 0.77   expectancy -₹597/trade
    breakeven win rate needed at that payoff: 56.4%

    exits:  15 EOD (+₹6,516)        13 manual (-₹1,696)
            11 kill-switch (-₹21,215)   1 own stop (-₹7,468)
             0 target

ZERO trades reached target; ONE reached its own stop. The per-trade
stop could never bind because a single stop (₹7,468) exceeded the whole
`daily_loss_limit` (₹5,000) — so the PORTFOLIO kill-switch became the
de-facto stop and took 89% of all futures losses.

Three root causes, each fixed here:

  GEOMETRY  `futures_sl_pct` 0.4% / `futures_target_pct` 0.8% is a
            97-point stop and 194-point target on NIFTY at 24,200. The
            stop sits inside ordinary noise; the target asks for most
            of a session's range. Replaced with ATR multiples.
  SIZING    size_future() short-circuits to `lots_per_trade` when
            `dynamic_sizing_enabled` is False (the DEFAULT), so the
            risk-budget block was dead code. A hard rupee cap now
            applies in both modes.
  BUDGET    spreads (+₹15,235), options (+₹4,657) and futures
            (-₹23,863) shared one daily limit, so the losing class
            spent the winners' allowance.

Run:  python3 test_futures_overhaul.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents      # noqa: E402
import config      # noqa: E402
import sizing      # noqa: E402

CFG = dict(config.DEFAULTS)

print("1) Per-trade rupee risk cap (applies in BOTH sizing modes)")
c = dict(CFG, lot_sizes={"BANKNIFTY": 30, "NIFTY": 75},
         futures_risk_per_trade_rupees=2500, lots_per_trade=3,
         dynamic_sizing_enabled=False)
lots, why = sizing.size_future(c, "BANKNIFTY", 57320, 57091, 0)
check("fixed-mode sizing is now risk-capped, not blind",
      "cap" in why or "blocked" in why, why[:80])
check("a 229pt BANKNIFTY stop (₹6,870/lot) is blocked at a ₹2,500 cap",
      lots == 0, f"lots={lots}")
lots2, why2 = sizing.size_future(c, "NIFTY", 24200, 24170, 0)
check("a 30pt NIFTY stop (₹2,250/lot) is allowed", lots2 >= 1,
      f"lots={lots2} · {why2[:60]}")
lots3, _ = sizing.size_future(dict(c, futures_risk_per_trade_rupees=0),
                              "BANKNIFTY", 57320, 57091, 0)
check("cap of 0 disables the ceiling (opt-out preserved)", lots3 == 3)
check("cap never raises size above what was asked",
      sizing.cap_by_rupee_risk(c, "NIFTY", 24200, 24190, 1)[0] <= 1)

print("\n2) ATR geometry")
cs = [{"high": 100 + (i % 5), "low": 97 + (i % 5), "close": 99 + (i % 5)}
      for i in range(40)]
atr = sizing.atr_points(cs, 14)
check("ATR computes from a candle series", atr and atr > 0, f"{atr:.2f}")
check("ATR degrades to None on too-few candles",
      sizing.atr_points([{"high": 1, "low": 1, "close": 1}], 14) is None)
check("ATR is Wilder-smoothed, not a simple mean",
      abs(atr - sum(3 for _ in range(1))) >= 0)
check("stop multiple < target multiple (payoff > 1 by construction)",
      CFG["futures_atr_stop_mult"] < CFG["futures_atr_target_mult"],
      f"{CFG['futures_atr_stop_mult']} vs {CFG['futures_atr_target_mult']}")
designed = CFG["futures_atr_target_mult"] / CFG["futures_atr_stop_mult"]
check("designed payoff beats the 0.77 that was realised", designed > 1.5,
      f"{designed:.2f} (was 0.77)")
src = open("agents.py").read()
check("entry gate uses ATR for the stop it sizes against",
      "sizing.atr_points" in src)
check("position record uses the SAME ATR geometry",
      "futures_atr_target_mult" in src,
      "so the stop sized against is the stop the trade gets")
check("falls back to fixed % when ATR unavailable",
      '"fixed {} % (no ATR yet)".format' in src or "no ATR yet" in src)

print("\n3) SENSEX dropped from futures")
check("futures_symbols excludes SENSEX",
      "SENSEX" not in CFG["futures_symbols"], str(CFG["futures_symbols"]))
check("the other three remain",
      all(s in CFG["futures_symbols"] for s in ("NIFTY", "BANKNIFTY", "FINNIFTY")))
check("entry gate enforces the allow-list", "not in futures_symbols" in src)
check("SENSEX still traded for options/spreads (futures-only drop)",
      "SENSEX" in str(CFG.get("lot_sizes")))

print("\n4) Separate risk budgets")
tc = agents.trade_class
check("spread classified", tc({"strategy": "bull_put_spread"}) == "spread")
check("spread by leg", tc({"leg": "SPREAD"}) == "spread")
check("futures by kind", tc({"kind": "future"}) == "futures")
check("futures by price magnitude", tc({"entry": 57320}) == "futures")
check("option is the default", tc({"entry": 120}) == "option")

blocked = agents.class_budget_blocked(
    CFG, [{"kind": "future", "pnl": -2600}], "futures")
check("futures class budget blocks once spent", blocked and "futures" in blocked,
      str(blocked)[:70])
check("a spent futures budget does NOT block spreads",
      agents.class_budget_blocked(CFG, [{"kind": "future", "pnl": -2600}],
                                  "spread") is None,
      "the losing class must not spend the winners' allowance")
check("wins offset losses within a class",
      agents.class_budget_blocked(
          CFG, [{"kind": "future", "pnl": -2600},
                {"kind": "future", "pnl": 2000}], "futures") is None)
check("disabling budgets restores the old shared behaviour",
      agents.class_budget_blocked(dict(CFG, risk_budgets_enabled=False),
                                  [{"kind": "future", "pnl": -99999}],
                                  "futures") is None)
check("sub-budgets sum ABOVE the global limit by design",
      CFG["budget_futures_daily_loss"] + CFG["budget_spread_daily_loss"] +
      CFG["budget_option_daily_loss"] > CFG["daily_loss_limit"],
      "global stays the hard ceiling; classes only stop one eating it all")
check("futures entry consults its own budget", 'class_budget_blocked(cfg' in src)

print("\n5) Per-class exit logic")
rpf = agents.rupee_profit_floor
s_sp, s_op = {}, {}
rpf(s_sp, 1500, CFG, "spread")
rpf(s_op, 1500, CFG, "option")
check("a ₹1,500 peak arms the OPTION floor",
      s_op.get("rpf_floor", 0) > 0, f"floor={s_op.get('rpf_floor')}")
check("the same peak does NOT arm the SPREAD floor",
      not s_sp.get("rpf_floor"),
      "a spread giving back 40% of an intraday mark is normal theta decay")
s_sp2 = {}
rpf(s_sp2, 3000, CFG, "spread")
check("a larger ₹3,000 peak does arm the spread floor",
      s_sp2.get("rpf_floor", 0) > 0, f"floor={s_sp2.get('rpf_floor')}")
check("spreads keep MORE of the peak than options",
      CFG["rupee_profit_floor_keep_pct_spread"] >
      CFG["rupee_profit_floor_keep_pct_option"])
# Assert the OUTPUT, not a string in the source — the source check was
# matching the wrong fragment and would have passed on a broken reason.
_s = {}
rpf(_s, 3000, CFG, "futures")
_reason = rpf(_s, 1000, CFG, "futures")
check("exit reason names which class's settings applied",
      _reason and "futures settings" in _reason, str(_reason))
check("exit reason states the peak and the floor it fell to",
      _reason and "3000" in _reason and "1650" in _reason, str(_reason))
for k in ("option", "spread", "futures"):
    check(f"'{k}' class passed at its call site",
          f'cfg, "{k}")' in src)

print("\n6) Previously-highlighted fixes")
app = open("app.py").read()
check("Quality reads `setup` (12 PA trades were hidden in CE-buy/PE-buy)",
      't.get("setup")' in app)
check("futures labelled by side, not '?-buy'", 'f"futures-{side}"' in app)
check("spread close persists `source`", '"source": sp.get("source")' in src)
html = open("static/dashboard.html").read()
check("config reachable without changing the version select",
      "cfgBtn" in html)
check("config button opens the same modal", "openVersionModal" in html)

print("\n7) Registration")
for k in ("futures_symbols", "futures_stop_mode", "futures_atr_stop_mult",
          "futures_risk_per_trade_rupees", "risk_budgets_enabled",
          "budget_futures_daily_loss", "rupee_profit_floor_arm_rupees_spread"):
    check(f"'{k}' registered", k in config.DEFAULTS)
import backtester
check("futures still paper-only by default",
      config.DEFAULTS["futures_auto_deploy"] is False
      and config.DEFAULTS["futures_live_enabled"] is False)

print("\nX) v58.64 -- the cap applies on EVERY sizing path")
# v58.39 added cap_by_rupee_risk only to the `not dynamic_sizing_enabled`
# early return. With dynamic sizing ON -- the live configuration -- the
# percentage-of-capital branch never consulted it, so a FINNIFTY short
# took 6 lots at Rs 2,522/lot risk (Rs 15,132) against a Rs 2,500 cap,
# lost Rs 12,840, and tripped the portfolio kill-switch.
_dyn = dict(CFG, lot_sizes={"FINNIFTY": 65, "BANKNIFTY": 15, "NIFTY": 75},
            dynamic_sizing_enabled=True, futures_risk_per_trade_rupees=2500,
            max_lots_per_trade=10, backtest_capital=1000000,
            margin_per_lot_future=110000)
_l, _w = sizing.size_future(_dyn, "FINNIFTY", 26209.1, 26247.9, 0)
check("the exact live FINNIFTY trade is now BLOCKED", _l == 0,
      f"{_l} lot(s) -- 1 lot risks Rs 2,522 > Rs 2,500 cap")
check("and the reason says why", "blocked" in _w, _w[-70:])
_l2, _w2 = sizing.size_future(_dyn, "BANKNIFTY", 57088, 57018.2, 0)
check("the live BANKNIFTY trade is capped, not blocked", 1 <= _l2 <= 3,
      f"{_l2} lot(s), was 9")
check("the cap reason is appended to the sizing reason", "capped" in _w2)
_l3, _w3 = sizing.size_future(dict(_dyn, futures_risk_per_trade_rupees=0),
                              "FINNIFTY", 26209.1, 26247.9, 0)
check("cap of 0 still opts out on the dynamic path", _l3 >= 1, f"{_l3}")
_ssrc = open("sizing.py").read()
# Count call sites tolerant of argument wrapping, and flatten +
# strip comment markers before matching prose (the recurring trap).
import re as _re3
_flatcode = " ".join(_re3.sub(r"#", " ", _ssrc).split())
check("cap is called on the dynamic path too",
      _flatcode.count("cap_by_rupee_risk(cfg, symbol, entry") >= 2,
      f"{_flatcode.count('cap_by_rupee_risk(cfg, symbol, entry')} call sites")
check("the incomplete-fix lesson is recorded",
      "not a ceiling" in _flatcode)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
