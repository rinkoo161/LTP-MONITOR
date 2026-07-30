"""v58.22+ — tests for the futures defense zone, per explicit request
after investigating the FINNIFTY futures loss (-7,468, entry 26135.1,
SL 26239.64, never went into profit).

Root cause identified previously: the only protection besides the
fixed entry-time SL was a trailing stop that requires the position to
ALREADY be in profit before engaging — so a position moving straight
against entry from the start (as this one did) had no defense until
the full, original stop was reached. Spreads already have an
analogous "defense zone" that tightens the loss limit as spot nears
the danger point, before a full breach; futures had no equivalent.

This adds that equivalent for futures, adapted for their linear
(no-gamma) price/SL structure: once an ADVERSE move consumes a
configured fraction of the ORIGINAL entry-to-stop distance, the stop
tightens closer to current price rather than waiting for the full
original stop — a one-shot tightening (never loosens, never re-
triggers), deliberately separate from the existing favourable
trailing mechanism (which only engages once already in profit).

Run:  python3 test_futures_defense_zone.py
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents
import config

src = open("agents.py").read()
cfg_src = open("config.py").read()

print("1) source-level: config keys are registered in both DEFAULTS "
     "and SettingsIn (the project's own established lesson — "
     "config.save() silently drops keys registered in only one)")
check("futures_defense_enabled in config.py DEFAULTS",
      '"futures_defense_enabled": True' in cfg_src)
check("futures_defense_zone_pct in config.py DEFAULTS",
      '"futures_defense_zone_pct": 40' in cfg_src)
check("futures_defense_tighten_pct in config.py DEFAULTS",
      '"futures_defense_tighten_pct": 50' in cfg_src)
app_src = open("app.py").read()
check("all three keys registered in SettingsIn",
      "futures_defense_enabled: bool | None = None" in app_src and
      "futures_defense_zone_pct: float | None = None" in app_src and
      "futures_defense_tighten_pct: float | None = None" in app_src)

print("\n2) source-level: initial_sl captured at entry, separate from "
     "the mutable sl field")
check('pos["initial_sl"] = pos["sl"] captured at entry',
      'pos["initial_sl"] = pos["sl"]' in src)


class FakeBus:
    def __init__(self):
        self.data = {}
        self.logs = []
        self.alerts = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, val):
        self.data[key] = val

    def log(self, name, msg):
        self.logs.append(msg)

    def alert(self, level, name, sym, msg):
        self.alerts.append(msg)


def make_position(side, entry, initial_sl, target):
    return {"symbol": "FINNIFTY", "kind": "future", "side": side,
           "lots": 1, "lot_size": 65, "entry": entry, "sl": initial_sl,
           "initial_sl": initial_sl, "target": target, "peak": entry,
           "pnl": 0.0, "margin": 110000, "opened": "14:48:12",
           "opened_date": "2026-07-28", "expiry": None,
           "order_id": "TEST-1", "paper": True, "mae": 0.0, "mfe": 0.0,
           "defended": False}


def run_monitor(pos, ltp):
    fake_self = types.SimpleNamespace()
    # 2026-07-29 — the mock needs this or the suite becomes WALL-CLOCK
    # DEPENDENT: _monitor_futures falls through to _futures_ai_check
    # only when the EOD branch does not fire, i.e. before 15:15 IST.
    # Run in the afternoon it passed; run at 00:06 it raised
    # AttributeError on a method the real ExecutionAgent has and the
    # SimpleNamespace does not. A test that passes or fails by time of
    # day is worse than one that fails outright.
    fake_self._futures_ai_check = lambda *a, **k: None
    fake_self.bus = FakeBus()
    fake_self.name = "execution"
    fake_self.exit_future = lambda sym, reason="manual exit": None
    futs = {"FINNIFTY": pos}
    fake_self.bus.set("futures_positions", futs)
    fake_self.bus.set(f"future_ohlc:FINNIFTY", {"close": ltp})
    agents.ExecutionAgent._monitor_futures(fake_self)
    return futs["FINNIFTY"], fake_self.bus


print("\n3) BEHAVIORAL VERIFICATION: reconstructing the exact reported "
     "scenario (SHORT, entry 26135.1, initial SL 26239.64 — a 104.54pt "
     "risk distance) — confirms the defense zone triggers once an "
     "adverse move consumes 40%+ of that distance, BEFORE the "
     "original stop is reached")
pos = make_position("SHORT", 26135.1, 26239.64, 25926.02)
risk_distance = 26239.64 - 26135.1   # 104.54

# Move 30% of the way adversely (short losing as price rises) — should NOT trigger yet
ltp_30pct = 26135.1 + risk_distance * 0.30
pos, bus = run_monitor(pos, ltp_30pct)
check("at 30% of the adverse risk distance consumed, defense has NOT "
     "triggered yet (still within the configured 40% zone threshold)",
      pos["defended"] is False, f"sl={pos['sl']}")
check("stop is still at its original level at 30%",
      pos["sl"] == 26239.64, str(pos["sl"]))

# Move to 65% of the way adversely — should trigger (zone_pct=40 means
# trigger once adverse_move >= risk_distance - zone, i.e. >= 60%)
ltp_65pct = 26135.1 + risk_distance * 0.65
pos, bus = run_monitor(pos, ltp_65pct)
check("at 65% of the adverse risk distance consumed, defense HAS "
     "triggered (past the 60% threshold implied by a 40% zone)",
      pos["defended"] is True, f"sl={pos['sl']}")
check("the stop was genuinely tightened (moved closer to current "
     "price, i.e. LOWER than the original 26239.64 for a SHORT)",
      pos["sl"] < 26239.64, f"new sl={pos['sl']}")
check("the stop is still on the correct (protective) side of current "
     "price for a SHORT — above the current ltp, not below it",
      pos["sl"] > ltp_65pct, f"sl={pos['sl']} ltp={ltp_65pct:.2f}")
check("a log message was emitted describing the defense trigger",
      any("defense triggered" in m for m in bus.logs), str(bus.logs))
check("an alert was raised", len(bus.alerts) == 1, str(bus.alerts))

print("\n4) BEHAVIORAL VERIFICATION: one-shot — a further adverse move "
     "after the defense already triggered does NOT tighten again")
sl_after_first_trigger = pos["sl"]
ltp_further = 26135.1 + risk_distance * 0.90
pos, bus2 = run_monitor(pos, ltp_further)
check("the stop does not move again after the one-shot trigger already "
     "fired (stays at the same tightened level)",
      pos["sl"] == sl_after_first_trigger, f"sl={pos['sl']}")
check("no second defense log message on this call",
      not any("defense triggered" in m for m in bus2.logs))

print("\n5) BEHAVIORAL VERIFICATION: mirror case for LONG — direction-"
     "awareness confirmed independently, not just assumed symmetric")
pos_long = make_position("LONG", 24000.0, 23900.0, 24200.0)
risk_distance_long = 100.0
ltp_long_65pct = 24000.0 - risk_distance_long * 0.65   # price falling against a LONG
pos_long, bus3 = run_monitor(pos_long, ltp_long_65pct)
check("LONG: defense triggers on a symmetric adverse move (price "
     "falling, not rising)",
      pos_long["defended"] is True, f"sl={pos_long['sl']}")
check("LONG: the tightened stop is HIGHER than the original (closer "
     "to current price from below), not lower",
      pos_long["sl"] > 23900.0, f"sl={pos_long['sl']}")
check("LONG: the tightened stop is still below current price (correct "
     "protective side)",
      pos_long["sl"] < ltp_long_65pct)

print("\n6) BEHAVIORAL VERIFICATION: a FAVOURABLE move (in profit) does "
     "NOT trigger the defense zone — confirms this is genuinely "
     "separate from the existing trailing mechanism, not overlapping "
     "or conflicting with it")
pos_profit = make_position("SHORT", 26135.1, 26239.64, 25926.02)
ltp_in_profit = 26135.1 - 50   # price fell — a SHORT profiting
pos_profit, bus4 = run_monitor(pos_profit, ltp_in_profit)
check("no defense trigger while in profit (adverse_move would be "
     "negative, correctly excluded)",
      pos_profit["defended"] is False)
check("no defense log message while in profit",
      not any("defense triggered" in m for m in bus4.logs))

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
