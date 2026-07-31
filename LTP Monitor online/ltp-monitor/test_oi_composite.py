"""Strategy 10 — OI Buildup/Covering Composite (v58.65).

The operator's own methodology, formalised from a direct specification.
Tests assert the RULES as stated, not a reinterpretation of them.

Key architectural point: this is the first strategy here that produces a
COMPOSITE position (future + credit spread + long option) from one
option-chain condition, and exits its legs INDEPENDENTLY. Nothing in the
system coordinated instruments before -- which is how futures lost
Rs 72,321 on 2026-07-30 while spreads made money.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import oi_composite as oc

def chain(atm=24300, sp=50, pe_states=None, ce_states=None, prem=120, churn=False):
    """Build a chain where each strike's quadrant can be set explicitly."""
    rows = []
    for i in range(-4, 5):
        k = atm + i * sp
        rows.append({"strike": k,
            "pe": {"state": (pe_states or {}).get(k, "long-buildup"),
                   "churn": churn, "ltp": prem, "oi": 1000, "oi_chg": 10},
            "ce": {"state": (ce_states or {}).get(k, "long-buildup"),
                   "churn": churn, "ltp": prem, "oi": 1000, "oi_chg": 10}})
    return {"atm": atm, "strikes": rows, "futures_stop_points": 30}

print("1) Bullish composite — the stated rule")
# "Long buildup in Future + Short buildup in PE (at ATM, two strikes OTM)
#  + Short covering in Call"
a = chain(pe_states={24300: "short-buildup", 24250: "short-buildup"},
          ce_states={24300: "short-covering", 24350: "short-covering"})
s, det = oc.detect_setup(a, "long")
check("fires on the exact stated condition", s is not None, det.get("why", "")[:70])
check("it is a bullish composite", s and s["kind"] == "bullish_composite")
check("future leg is LONG", s and s["future_side"] == "LONG")
check("sells the ATM put that has the buildup", s and s["sell_pe"] == 24300,
      f"sell_pe={s and s.get('sell_pe')}")
check("buys 3 strikes further OTM", s and s["buy_pe"] == 24300 - 3 * 50,
      f"buy_pe={s and s.get('buy_pe')}")
check("long leg is a CE at ATM (highest delta)",
      s and s["long_leg_side"] == "ce" and s["long_leg_strike"] == 24300)

print("\n2) Bearish mirror — 'and vice versa'")
b = chain(ce_states={24300: "short-buildup", 24350: "short-buildup"},
          pe_states={24300: "short-covering", 24250: "short-covering"})
s2, d2 = oc.detect_setup(b, "short")
check("fires on the mirror condition", s2 is not None, d2.get("why", "")[:60])
check("it is a bearish composite", s2 and s2["kind"] == "bearish_composite")
check("future leg is SHORT", s2 and s2["future_side"] == "SHORT")
check("sells the ATM call with the buildup", s2 and s2["sell_ce"] == 24300)
check("buys 3 strikes further OTM on the call side",
      s2 and s2["buy_ce"] == 24300 + 3 * 50)
check("long leg is a PE at ATM", s2 and s2["long_leg_side"] == "pe")

print("\n3) It does NOT fire without every condition")
check("no fire when the future is not in long buildup",
      oc.detect_setup(a, "short")[0] is None or
      oc.detect_setup(a, "short")[0]["kind"] != "bullish_composite")
check("no fire without CE short-covering",
      oc.detect_setup(chain(pe_states={24300: "short-buildup"}), "long")[0] is None)
check("no fire without PE short-buildup",
      oc.detect_setup(chain(ce_states={24300: "short-covering"}), "long")[0] is None)
_, dn = oc.detect_setup(chain(), "long")
check("a rejection still explains itself", "no composite" in dn["why"], dn["why"][:60])
check("the rejection names what WAS and was not present",
      "PE short-buildup at" in dn["why"] and "CE short-covering at" in dn["why"],
      "an almost-setup is the interesting case")

print("\n4) Short condor — writers on both sides")
c = chain(pe_states={24300: "short-buildup", 24250: "short-buildup"},
          ce_states={24300: "short-buildup", 24350: "short-buildup"})
s3, d3 = oc.detect_setup(c, "long")
check("both-sides buildup gives a condor, not a directional trade",
      s3 and s3["kind"] == "short_condor", s3 and s3["kind"])
check("condor is direction-neutral", s3 and s3["direction"] == 0)
check("it has all four legs",
      s3 and all(k in s3 for k in ("sell_pe", "sell_ce", "buy_pe", "buy_ce")))
check("wings are hedged outward from the shorts",
      s3 and s3["buy_pe"] < s3["sell_pe"] and s3["buy_ce"] > s3["sell_ce"])
check("condor is checked BEFORE the directional cases",
      s3["kind"] == "short_condor",
      "it is a strictly stronger condition and would otherwise be masked")
check("condor can be switched off",
      oc.detect_setup(c, "long", {"condor_enabled": 0})[0] is None
      or oc.detect_setup(c, "long", {"condor_enabled": 0})[0]["kind"] != "short_condor")

print("\n5) The churn filter")
ch = chain(pe_states={24300: "short-buildup"}, ce_states={24300: "short-covering"},
           churn=True)
check("a churned strike is not treated as real positioning",
      oc.detect_setup(ch, "long")[0] is None,
      "high volume + small net OI change is weak hands, not writers")
check("disabling the filter lets it through",
      oc.detect_setup(ch, "long", {"require_churn_filter": 0})[0] is not None)

print("\n6) The 2% cap, and WHICH leg binds")
lots, si = oc.size_composite(s, a, 1000000, 75)
check("sizing returns lots", isinstance(lots, int))
check("budget is 2% of capital", si["budget"] == 20000, str(si.get("budget")))
check("it reports the per-lot composite risk", si["per_lot_risk"] > 0,
      f"Rs {si.get('per_lot_risk'):,}")
check("it names the BINDING leg", si.get("binding_leg") in
      ("spread", "long_option", "future"), str(si.get("binding_leg")))
check("it quantifies how much that leg consumes", si.get("binding_pct") is not None,
      f"{si.get('binding_pct')}%")
check("all three legs are priced separately",
      set(si["legs"]) == {"spread", "long_option", "future"}, str(si["legs"]))
check("lots never exceed max_concurrent",
      oc.size_composite(s, a, 10000000, 75)[0] <= oc.DEFAULTS["max_concurrent"],
      "even a 10x book cannot open more than the concurrency limit")
check("cost is stated per round trip", "round trip" in si["cost"] or "lot" in si["cost"])

print("\n7) Exit rules — each stated rule, separately")
pos = dict(s, direction=+1)
ex = oc.exit_reasons(pos, a, "unwinding")
check("'exit as notice long covering in Future'",
      any(leg == "future" for leg, _ in ex), str(ex))
a_ce_build = chain(pe_states={24300: "short-buildup"},
                   ce_states={24300: "long-buildup"})
ex2 = oc.exit_reasons(pos, a_ce_build, "long")
check("'Exit from bought CE when build-up starts in that CE'",
      any(leg == "long_option" for leg, _ in ex2), str(ex2))
a_pe_cover = chain(pe_states={24300: "short-covering"},
                   ce_states={24300: "short-covering"})
ex3 = oc.exit_reasons(pos, a_pe_cover, "long")
check("'Exit from both legs when notice covering in Put'",
      any(leg == "spread_both" for leg, _ in ex3), str(ex3))
check("exits are per-LEG, not one verdict",
      isinstance(ex3, list),
      "no existing exit path here could express 'close one leg, keep another'")
check("a healthy position exits nothing", oc.exit_reasons(pos, a, "long") == [])

print("\n8) The time-value roll")
# "when the market goes up, exit from the Sell PE side, keep bought PE
#  and again Short PE ATM"
moved = chain(atm=24400, pe_states={24400: "short-buildup"})
r = oc.roll_short_leg(pos, moved, "long")
check("a favourable move triggers a roll", r is not None, r and r["why"][:60])
check("it closes the OLD short", r and r["close"]["strike"] == 24300)
check("it KEEPS the bought leg", r and r["keep"]["strike"] == pos["buy_pe"])
check("it re-shorts at the NEW ATM", r and r["open"]["strike"] == 24400)
check("no roll when price has not moved favourably",
      oc.roll_short_leg(pos, chain(atm=24200, pe_states={24200: "short-buildup"}),
                        "long") is None)
check("no roll unless writers are active at the new ATM",
      oc.roll_short_leg(pos, chain(atm=24400), "long") is None,
      "re-shorting where nobody is writing defeats the purpose")

print("\n9) Robustness and stated assumptions")
check("empty chain degrades, does not raise",
      oc.detect_setup({}, "long")[0] is None)
check("missing ATM degrades", oc.detect_setup({"strikes": []}, "long")[0] is None)
check("spacing is MEASURED from the chain, not hard-coded",
      oc._spacing(chain(atm=57100, sp=100)["strikes"]) == 100,
      "an exchange changing spacing must not silently break it")
SRC = open("oi_composite.py").read()
FLAT = " ".join(SRC.split())
check("the '1:3 to 1:5' ambiguity is recorded, not silently guessed",
      "reads equally as a risk:reward target" in FLAT)
check("both readings are implemented",
      "max_concurrent" in oc.DEFAULTS and "rr_target" in oc.DEFAULTS)
check("the Rs 50 per-leg assumption is stated and switchable",
      "cost_is_per_lot" in oc.DEFAULTS and "per ORDER, not per lot" in FLAT)
check("the binding 2% arithmetic is documented with real numbers",
      "19,462" in FLAT and "1.03 lots" in FLAT)
check("auto_deploy defaults OFF (observe first)",
      oc.DEFAULTS["auto_deploy"] is False)
check("all binary params flip rather than step",
      all(BOUNDS_V[:2] == (0, 1) for k, BOUNDS_V in oc.BOUNDS.items()
          if k in ("require_churn_filter", "condor_enabled")))

print("\n10) Wiring — observation is unconditional")
AG = open("agents.py").read()
check("StrategyAgent observes S10", "_oi_composite_observe" in AG)
check("it runs every cycle", "self._oi_composite_observe(config.load())" in AG)
i_obs = AG.index("setup, detail = oic.detect_setup")
i_gate = AG.index('if not p.get("auto_deploy", False):')
check("EVALUATION happens before the auto_deploy gate", i_obs < i_gate,
      "the S8 lesson: a gate governs TRADING, never observation")
check("state is published to the bus for the UI", 'f"oi_composite:{sym}"' in AG)
check("a detector failure cannot kill the cycle", "S10 detect FAILED" in AG)
check("the whole observe pass is wrapped too", "S10 observe cycle FAILED" in AG)
check("auto_deploy honestly reports the executor is unbuilt",
      "executor is not built yet" in AG,
      "better than silently doing nothing when the switch is on")

import config
for k in ("oi_composite_enabled", "oi_composite_auto_deploy",
          "oi_composite_risk_pct", "oi_composite_max_concurrent",
          "oi_composite_rr_target", "oi_composite_cost_per_leg",
          "oi_composite_spread_width_strikes", "oi_composite_condor_enabled"):
    check(f"'{k}' registered", k in config.DEFAULTS)
check("risk_pct default is the stated 2%", config.DEFAULTS["oi_composite_risk_pct"] == 2.0)
check("auto_deploy default OFF", config.DEFAULTS["oi_composite_auto_deploy"] is False)
check("spread width default is the stated 3 strikes",
      config.DEFAULTS["oi_composite_spread_width_strikes"] == 3)
check("OTM strikes checked default is the stated 2",
      config.DEFAULTS["oi_composite_otm_strikes_checked"] == 2)

print("\n11) Futures quadrant strings — the producer/consumer mismatch")
# This is the bug the earlier tests could not catch: they passed "long"
# by hand, while MarketDataAgent publishes "long_buildup". A test that
# invents its input cannot detect a mismatch with the real producer.
AGSRC = open("agents.py").read()
import re as _re
_produced = set(_re.findall(r'quadrant, trend = "(\w+)"', AGSRC))
check("the agent's real quadrant strings were found in source",
      len(_produced) >= 3, str(sorted(_produced)))
for q in sorted(_produced):
    check(f"'{q}' normalises to a canonical form",
          oc.normalise_future_quadrant(q) is not None, oc.normalise_future_quadrant(q))
check("the agent's long-buildup string reaches the bullish branch",
      oc.detect_setup(a, "long_buildup")[0] is not None,
      "detect_setup compared against 'long' and would NEVER have matched")
check("the trend-key form still works too",
      oc.detect_setup(a, "long")[0] is not None,
      "future_oi_trend publishes long/short")
check("the agent's unwinding string triggers the futures exit",
      any(leg == "future" for leg, _ in
          oc.exit_reasons(dict(s, direction=+1), a, "long_unwinding")),
      "exit_reasons compared against 'long-unwinding' with a hyphen")
check("None is handled", oc.normalise_future_quadrant(None) is None)
check("the mismatch is documented as a silent-failure class",
      "would have been silent" in " ".join(open("oi_composite.py").read().split()))

print("\n12) Backtest replay — scope stated, not implied")
check("replay exists", hasattr(oc, "replay"))
OSRC = " ".join(open("oi_composite.py").read().split())
check("it distinguishes full from chain_only mode",
      '"chain_only"' in OSRC and '"full"' in OSRC)
check("chain_only says it OVERSTATES the count",
      "OVERSTATES the trigger count" in OSRC,
      "assuming the futures leg agreed inflates every number")
check("no-data returns a reason, not an empty success",
      "no chain_snapshots for this day" in OSRC)
r = oc.replay("NOSUCHSYM", "2026-01-01")
check("a symbol with no archive degrades cleanly", r["mode"] == "no_data", str(r)[:60])
check("ATM inference is isolated and explained",
      "_infer_atm" in OSRC and "property of how the archive is written" in OSRC)
import history as _hist
check("chain_series derives `chg` from consecutive snapshots",
      "chg` is DERIVED" in " ".join(open("history.py").read().split()),
      "chg is not stored but the quadrant classifier needs it")
check("futures OI is now archived so mode=full becomes possible",
      hasattr(_hist, "log_future_oi") and hasattr(_hist, "future_oi_series"))

print("\n13) The archive stores INPUTS, not the derived state")
# 4,112 archived snapshots across 5 days produced ZERO setups -- in the
# mode that is supposed to OVERSTATE. Cause: chain_snapshots has no
# `state` column, so a replay that merely reproduced the row SHAPE fed
# the detector state=None on every leg. Zero was guaranteed, not
# measured.
import history as _h2, importlib as _il3, time as _t3, datetime as _dt3
_il3.reload(_h2)
_c = _h2._conn()
_cols = [r[1] for r in _c.execute("PRAGMA table_info(chain_snapshots)")]
check("the archive genuinely has no 'state' column", "state" not in _cols,
      "so it MUST be derived on read")
check("it does have the raw inputs the classifier needs",
      all(k in _cols for k in ("ltp", "oi_chg")))
_ts = int(_t3.time()) // 60 * 60
_c.execute("DELETE FROM chain_snapshots WHERE symbol='ZZTEST'")
for _off, _pe, _ce, _oi_pe, _oi_ce in ((0, 120, 120, 0, 0), (60, 110, 130, 50000, -50000)):
    for _k in (24250, 24300, 24350):
        _c.execute("INSERT INTO chain_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("ZZTEST", _k, "pe", _ts + _off, _pe, 1e6, _oi_pe, 1000, 15, -0.5, 0, 0, 0, 0, 0))
        _c.execute("INSERT INTO chain_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("ZZTEST", _k, "ce", _ts + _off, _ce, 1e6, _oi_ce, 1000, 15, 0.5, 0, 0, 0, 0, 0))
_c.commit(); _c.close()
_day = _dt3.datetime.fromtimestamp(_ts).strftime("%Y-%m-%d")
_ser = _h2.chain_series("ZZTEST", _day)
check("chain_series returns snapshots", len(_ser) >= 2, f"{len(_ser)}")
_mid = _ser[-1]["strikes"][len(_ser[-1]["strikes"]) // 2]
check("PE state is DERIVED on read", _mid["pe"]["state"] == "short-buildup",
      str(_mid["pe"].get("state")))
check("CE state is DERIVED on read", _mid["ce"]["state"] == "short-covering",
      str(_mid["ce"].get("state")))
check("churn is derived too", "churn" in _mid["pe"])
_HS = " ".join(open("history.py").read().split())
check("it reuses analyzer.classify_leg, not a reimplementation",
      "analyzer as _an" in _HS and "_an.classify_leg" in _HS,
      "a replay that classifies differently from production is worthless")
check("the zero-setup failure is recorded", "4,112 snapshots" in _HS)
_r13 = oc.replay("ZZTEST", _day, 1000000,
                 futures_series=[{"ts": _ts + 60, "quadrant": "long_buildup"}])
check("the replay now FIRES on archived data", _r13["n_setups"] >= 1,
      f"mode={_r13['mode']} setups={_r13['n_setups']}")
check("and reports mode=full when futures OI is present",
      _r13["mode"] == "full")
_cz = _h2._conn(); _cz.execute("DELETE FROM chain_snapshots WHERE symbol='ZZTEST'")
_cz.commit(); _cz.close()

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
