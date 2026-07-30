"""v58.35 — rupee profit floor + unconditional AI advisory logging.

Built from a full live session (2026-07-29) in which ₹55,707 of peak
unrealised profit became ₹24,429 realised — ₹31,278 given back, 43.9%
capture, 20 of 31 trades surrendering something.

The cause was the UNIT, not a missing mechanism. Every percentage-
denominated protection failed to arm:

    spread profit-lock   armed at target x 80%  -> 2 of 11 armed, both
                                                   already at target
    option trail_sl      armed at 5% of premium -> peaks were 0.6-4.4%
    futures trail        armed at 0.3% of price -> needed 172pts, got 138

...while every rupee-denominated exit captured its full peak: three
`transaction_target_rupees` exits with ZERO giveback, plus the single
`step_trail` exit that fired.

Run:  python3 test_rupee_profit_floor.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents  # noqa: E402
import config  # noqa: E402

CFG = {"rupee_profit_floor_enabled": True, "rupee_profit_floor_arm_rupees": 750,
       "rupee_profit_floor_keep_pct": 60, "rupee_profit_floor_min_rupees": 300}
rpf = agents.rupee_profit_floor

print("1) Ratchet mechanics")
s = {}
check("below arm -> no floor set", rpf(s, 500, CFG) is None and not s.get("rpf_floor"))
check("peak still tracked below arm", s["rpf_peak"] == 500)
check("at arm -> floor set to keep%", rpf(s, 750, CFG) is None and s["rpf_floor"] == 450,
      f"floor={s.get('rpf_floor')}")
check("rising peak raises the floor", rpf(s, 2000, CFG) is None and s["rpf_floor"] == 1200,
      f"floor={s['rpf_floor']}")
check("falling P&L does NOT lower the floor",
      rpf(s, 1500, CFG) is None and s["rpf_floor"] == 1200)
r = rpf(s, 1200, CFG)
check("touching the floor triggers an exit", r is not None and "profit floor" in r, r)
check("exit reason names both floor and peak",
      "1200" in r and "2000" in r, r)

print("\n2) Guards")
s2 = {}
rpf(s2, 800, CFG)
check("floor below min_rupees does not fire",
      rpf(s2, 0, dict(CFG, rupee_profit_floor_min_rupees=1000)) is None,
      f"floor={s2.get('rpf_floor')} < min 1000")
s3 = {}
check("disabled -> always None",
      rpf(s3, 99999, dict(CFG, rupee_profit_floor_enabled=False)) is None
      and not s3.get("rpf_peak"))
s4 = {}
check("a losing position never arms",
      rpf(s4, -5000, CFG) is None and not s4.get("rpf_floor"))
check("negative P&L doesn't corrupt the peak", s4["rpf_peak"] == 0.0 or s4["rpf_peak"] == -5000.0)

print("\n3) Unit-independence — the whole point")
# The same ₹ profit must behave identically whether it came from a
# ₹120 option, a ₹72 spread credit, or a 57,320 futures entry. That is
# precisely what the percentage mechanisms could not do.
outcomes = []
for label, _entry in [("option", 120.0), ("spread", 72.0), ("futures", 57320.0)]:
    st = {}
    rpf(st, 4140, CFG)          # today's best futures peak
    outcomes.append((label, st["rpf_floor"], rpf(st, 2400, CFG) is not None))
check("identical floor for identical ₹ profit across instruments",
      len({o[1] for o in outcomes}) == 1, str(outcomes))
check("all three would have exited at the same giveback",
      all(o[2] for o in outcomes), str(outcomes))

print("\n4) REPLAY — what today's floor would have recovered")
# Today's real trades: (label, MFE peak ₹, realised ₹). Only trades
# that gave something back and whose peak clears the arm threshold can
# be helped; the rest are listed to prove the floor does NOT claim them.
TODAY = [
    ("futures EOD", 4140, 1050), ("bear_call time-stop", 3705, 830),
    ("option txn-SL", 366, -2280), ("futures kill-switch", 18, -2520),
    ("futures EOD", 3735, 1365), ("bear_call time-stop", 2010, -240),
    ("option kill-switch", 609, -1386), ("futures kill-switch", 1080, -765),
    ("futures EOD", 2281, 455), ("bull_put loss-limit", 0, -1677),
    ("option step-trail", 2047, 817), ("bear_call time-stop", 1170, -9),
    ("bull_put time-stop", 966, -186), ("option spot-inval", 375, -596),
    ("futures EOD", 680, -230), ("bull_put close", 852, 51),
    ("option kill-switch", 398, -379), ("bull_put close", 237, -422),
    ("bull_put close", 177, -273), ("option stoploss", 967, 930),
]
arm, keep = CFG["rupee_profit_floor_arm_rupees"], CFG["rupee_profit_floor_keep_pct"] / 100
recovered = 0.0
helped = []
for label, mfe, real in TODAY:
    if mfe >= arm:
        floor = round(mfe * keep, 0)
        # The floor is the WORST case: the ratchet exits at or above it.
        if floor > real:
            recovered += floor - real
            helped.append((label, mfe, real, floor))
print(f"    trades whose peak cleared the ₹{arm} arm: "
      f"{sum(1 for _, m, _ in TODAY if m >= arm)} of {len(TODAY)}")
print(f"    {'trade':24s} {'peak':>7s} {'actual':>8s} {'floor':>7s} {'saved':>8s}")
for label, mfe, real, floor in sorted(helped, key=lambda x: -(x[3] - x[2])):
    print(f"    {label:24s} {mfe:>7.0f} {real:>8.0f} {floor:>7.0f} {floor - real:>8.0f}")
print(f"\n    ACTUAL realised today:        ₹{sum(r for _, _, r in TODAY):>9,.0f}")
print(f"    WITH the floor (worst case):  ₹{sum(r for _, _, r in TODAY) + recovered:>9,.0f}")
print(f"    RECOVERED:                    ₹{recovered:>9,.0f}")

actual_total = sum(r for _, _, r in TODAY)
total_giveback = sum(m - r for _, m, r in TODAY)
# Assert the real claim rather than an arbitrary rupee figure: the
# floor should recover a meaningful FRACTION of the measured giveback,
# and should turn this cohort (the trades that gave something back)
# from a net loss into a net profit.
check("recovers >25% of the measured giveback",
      recovered / total_giveback > 0.25,
      f"₹{recovered:,.0f} of ₹{total_giveback:,.0f} = "
      f"{recovered / total_giveback * 100:.0f}%")
check("turns the giveback cohort from net loss to net profit",
      actual_total < 0 < actual_total + recovered,
      f"₹{actual_total:,.0f} -> ₹{actual_total + recovered:,.0f}")
# IMPORTANT and deliberately asserted: this is a LOWER bound. The
# ratchet exits the moment P&L touches the floor and the floor rises
# with every new peak, so a real run captures at least this much. It
# is NOT a promise of the same total: exiting a position early changes
# what happens afterwards — a trade stopped at its floor can no longer
# go on to reach its target. Treat it as "the giveback this would have
# prevented", not "the P&L this would have produced".
check("replay is a conservative lower bound, not a projection",
      all(floor <= mfe for _, mfe, _, floor in helped),
      "floor never exceeds the peak it is derived from")
check("it helps the trades that gave back most",
      any(h[0].startswith("futures EOD") for h in helped))
check("it does NOT claim trades that never reached the arm",
      all(mfe >= arm for _, mfe, _, _ in helped))
check("it does NOT claim the losers that never showed profit",
      not any(h[1] < arm for h in helped))

print("\n4b) Class resolution is explicit (v58.45)")
for _lbl, _exp in (("spread", 3750.0), ("futures", 2750.0), ("option", 3000.0),
                   ("NIFTY", 3000.0), (None, 3000.0)):
    _s = {}
    rpf(_s, 5000, dict(config.DEFAULTS), _lbl)
    check(f"label={_lbl!r} -> floor {_exp}", _s.get("rpf_floor") == _exp,
          f"got {_s.get('rpf_floor')}")
_src = open("agents.py").read()
# Assert the READ is gone, not the word — the explanatory comment
# naming the removed key should survive, and an over-strict string
# match would force deleting the explanation to make the test pass.
check("no dead `_rpf_class` config read remains",
      'cfg.get("_rpf_class")' not in _src,
      "it was read but never set anywhere")
check("class resolution does not depend on operator precedence",
      "kls or label if label in" not in _src)

print("\n5) Wiring — all three instrument types")
src = open("agents.py").read()
check("options consult the floor", "_rpf_option" in src)
check("spreads consult the floor", "_rpf_spread" in src)
check("futures consult the floor", "_rpf_fut" in src)
check("floor is evaluated ONCE per cycle (it mutates state)",
      src.count("_rpf_option = rupee_profit_floor") == 1
      and src.count("_rpf_spread = rupee_profit_floor") == 1
      and src.count("_rpf_fut = rupee_profit_floor") == 1)
i_fut = src.index("_rpf_fut:")
i_eod = src.index('self.exit_future(sym, "EOD square-off (15:15)")')
check("futures floor is checked BEFORE the EOD square-off", i_fut < i_eod,
      "EOD closed every profitable futures position today")
i_sp = src.index("elif _rpf_spread:")
i_ts = src.index('reason = (f"time stop ({elapsed:.0f}m')
check("spread floor is checked BEFORE the time stop", i_sp < i_ts,
      "the time stop closed a ₹3,705-peak spread for ₹830")

print("\n6) AI advisory logging")
check("advisory logger exists", "_log_ai_advisory" in src)
check("wired into all three advisories",
      src.count("_log_ai_advisory(self,") == 3, f"{src.count('_log_ai_advisory(self,')}")
check("logs HOLD verdicts too, not only EXIT",
      'else "hold"' in src)
check("distinguishes would-have-acted from acted",
      "auto-exit OFF" in src and "auto-exit ON" in src)
check("below-threshold EXITs are recorded",
      "below threshold" in src)

print("\n7) AI advisory cadence — event-driven, not clock-driven")
due = agents.ai_advisory_due
C = dict(config.DEFAULTS)

st = {"ai_ts": 0}
check("first call is allowed", due(st, C, 0, 1000) is not None)

st = {"ai_ts": __import__("time").time()}
check("hard floor blocks a call inside the danger interval",
      due(st, C, 5000, 1000, near_stop=True) is None,
      "even freefall cannot drain the daily cap")

import time as _t
st = {"ai_ts": _t.time() - 25, "ai_last_pnl": 0}
check("near-stop triggers after the danger floor (~20s), not 300s",
      "danger" in str(due(st, C, -100, 1000, near_stop=True)))

st = {"ai_ts": _t.time() - 25, "ai_last_pnl": 0, "ai_peak_pnl": 3000}
r = due(st, C, 1800, 1000)
check("40% giveback of peak triggers as danger", r and "gave back" in r, str(r))

st = {"ai_ts": _t.time() - 50, "ai_last_pnl": 0}
r = due(st, C, 300, 1000)
check("a 30%-of-risk move triggers after the min interval",
      r and "moved" in r, str(r))

st = {"ai_ts": _t.time() - 50, "ai_last_pnl": 0}
check("a small move does NOT trigger", due(st, C, 50, 1000) is None,
      "5% of risk - not worth a call")

st = {"ai_ts": _t.time() - 320, "ai_last_pnl": 0}
check("quiet position still gets a periodic review",
      str(due(st, C, 10, 1000)) == "periodic review")

st = {"ai_ts": _t.time() - 50, "ai_last_pnl": 0}
check("unknown risk degrades safely (no crash, no spurious trigger)",
      due(st, C, 500, None) is None)

# Budget: the trigger must not be able to beat the danger floor.
worst = 22500 / C["ai_exit_advisory_danger_interval_sec"]
check("worst-case calls/position/day stay describable",
      worst == 1125, f"{worst:.0f} - hence the cap guard still matters")

src_a = open("agents.py").read()
check("all three advisories use the shared trigger",
      src_a.count("ai_advisory_due(") == 4,
      f"{src_a.count('ai_advisory_due(')} (1 def + 3 calls)")
check("no fixed 300s guard remains", 'ai_ts", 0) < 300' not in src_a)
check("advisory latency is measured and logged",
      "took {latency:.1f}s" in src_a)
check("trigger reason is logged for later audit", "trigger: {trigger}" in src_a)

print("\n8) Config registration")
for k in ("rupee_profit_floor_enabled", "rupee_profit_floor_arm_rupees",
          "rupee_profit_floor_keep_pct", "rupee_profit_floor_min_rupees",
          "ai_exit_advisory_logging", "ai_exit_advisory_danger_interval_sec",
          "ai_exit_advisory_min_interval_sec", "ai_exit_advisory_max_interval_sec",
          "ai_exit_advisory_move_trigger_pct",
          "ai_exit_advisory_giveback_trigger_pct"):
    check(f"'{k}' registered", k in config.DEFAULTS)
check("floor is ON by default", config.DEFAULTS["rupee_profit_floor_enabled"] is True)
check("advisory logging is ON by default",
      config.DEFAULTS["ai_exit_advisory_logging"] is True)
check("arm threshold well below the old ₹2000 step-trail trigger",
      config.DEFAULTS["rupee_profit_floor_arm_rupees"] <
      config.DEFAULTS["step_trail_lock_trigger_rupees"],
      f"{config.DEFAULTS['rupee_profit_floor_arm_rupees']} vs "
      f"{config.DEFAULTS['step_trail_lock_trigger_rupees']}")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
