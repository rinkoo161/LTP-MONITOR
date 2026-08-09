#!/usr/bin/env python3
"""test_promotion_gate.py — v59.0 item 17.

The gate's whole value is that it treats bias and variance differently.
A test that only checked "big number fails, small number passes" would
pass just as happily on `required = 200` hardcoded, so these check the
STRUCTURE: that the statistical term shrinks with n, that the cost term
does not, and that the proxy's measured mean is never netted out.
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_promotion_gate")

import promotion_gate as pg

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


# --- the statistical term shrinks as sd/sqrt(n) -------------------------
m100 = pg.required_margin(100, bias=0.0)
m400 = pg.required_margin(400, bias=0.0)
check("stat margin halves when n quadruples", abs(m100 / m400 - 2.0) < 1e-6,
      f"n=100 -> {m100:.1f}, n=400 -> {m400:.1f}")
check("stat margin is k*sd/sqrt(n)",
      abs(m100 - 2 * pg.PROXY_SD_PER_TRADE / 10.0) < 1e-6, f"{m100:.2f}")

# --- the cost term does NOT shrink with n -------------------------------
b = 50.0
check("cost bias is n-invariant",
      abs((pg.required_margin(100, bias=b) - pg.required_margin(100, bias=0))
          - (pg.required_margin(9000, bias=b) - pg.required_margin(9000, bias=0)))
      < 1e-9,
      "bias contributes the same rupees at n=100 and n=9000")

# --- the proxy's +102 mean must never be netted out ---------------------
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "promotion_gate.py")).read()
code = "\n".join(l for l in src.splitlines()
                 if not l.strip().startswith("#"))
code = code.split('"""')[-1]        # drop the module docstring
check("no +102 proxy mean applied in code", "102" not in code,
      "the measured mean is evidence of bias, not a correction to apply")

# --- a strategy on the line ---------------------------------------------
# own_sd is REQUIRED now: the applied margin needs a sampling term.
ok, d = pg.evaluate("x", "NIFTY", 355, 355 * 900.0, own_sd=2886)
check("a genuinely large edge passes", ok, f"headroom {d['headroom']:+.0f}")
ok2, d2 = pg.evaluate("x", "SENSEX", 341, 341 * 4.0, own_sd=2399)
check("marginal case fails", not ok2, f"headroom {d2['headroom']:+.0f}")

# --- unmeasurable must not read as passed -------------------------------
okN, dN = pg.evaluate("x", "NIFTY", 355, 355 * 9000.0)
check("no own_sd is DENIED even with a huge edge", not okN,
      dN.get("reason", ""))
check("and says why rather than returning a number",
      dN.get("required") is None and "dispersion" in dN.get("reason", ""))

# --- the applied form is the calibrated one (item 26) -------------------
check("evaluate() reports the calibrated form",
      d.get("margin_form") == "calibrated", str(d.get("margin_form")))
# v59.66 — evaluate() defaults k to the DEFLATED max-of-N value, not
# DEFAULT_K=2. Compare at the k evaluate actually reported, scraped from
# its own payload — inventing a k here is the producer/consumer mismatch
# CLAUDE.md warns about.
_cal = pg.required_margin_calibrated(355, 2886, "NIFTY", k=d["k"])
check("evaluate() agrees with required_margin_calibrated at its own k",
      abs(d["required"] - _cal) < 1e-6, f"{d['required']:.2f} vs {_cal:.2f}")
check("default k is the deflated one, floored at the pre-commitment",
      d["k"] >= pg.PRECOMMITTED_K and d.get("k_source", "").startswith("deflated"),
      f"k={d['k']:.3f} ({d.get('k_source')})")
check("the superseded form is kept for the record only",
      d.get("legacy_required") is not None
      and d["required"] > d["legacy_required"],
      "applied must be stricter than what it replaced")
# The calibration floor must NOT shrink as a strategy accumulates trades.
_f1 = pg.required_margin_calibrated(10 ** 6, 2886, "NIFTY", bias=0.0)
check("a calibration floor survives infinite n", _f1 > 250,
      f"₹{_f1:.0f} — no amount of trading fixes a 4-session bias estimate")

# --- same net/trade, smaller sample => harder gate -----------------------
_, small = pg.evaluate("x", "NIFTY", 40, 40 * 150.0, own_sd=2886)
_, big = pg.evaluate("x", "NIFTY", 4000, 4000 * 150.0, own_sd=2886)
check("smaller sample demands more", small["required"] > big["required"],
      f"n=40 needs ₹{small['required']:.0f}, n=4000 needs ₹{big['required']:.0f}")

# --- provisional labelling travels with the number ----------------------
check("sd carries provenance", "provisional" in d["sd_provenance"].lower())
check("cost carries provenance", "measured" in d["cost_provenance"].lower())
check("both provenances are in every payload",
      "sd_provenance" in d2 and "cost_provenance" in d2)

# --- independent errors add in QUADRATURE, not linearly -----------------
c = pg.combined_sd(own_sd=1000.0, sd=1000.0)
check("combined sd is quadrature", abs(c - (2 ** 0.5) * 1000.0) < 1e-6,
      f"{c:.1f} (linear addition would be 2000)")
check("no own_sd falls back to the model term alone",
      pg.combined_sd(None) == pg.PROXY_SD_PER_TRADE)
check("combining raises the bar",
      pg.required_margin(300, bias=0, own_sd=2886) >
      pg.required_margin(300, bias=0), "the lenient case under-protects")

# --- real pnl_sd must be preferred over the approximation ---------------
m = {"trades": 100, "net_pnl": 1000, "pnl_sd": 2500,
     "win_rate": 50, "avg_win": 900, "avg_loss": -700}
sd, src = pg.own_sd_from(m)
check("measured pnl_sd wins over the win/loss approximation",
      sd == 2500 and src == "measured", f"{sd} from {src}")
sd2, src2 = pg.own_sd_from({k: v for k, v in m.items() if k != "pnl_sd"})
check("falls back when pnl_sd absent", sd2 and "lower bound" in src2, src2)
check("the fallback is flagged as permissive", "permissive" in src2, src2)

# --- backtester now records the real sd ---------------------------------
here = os.path.dirname(os.path.abspath(__file__))
bt_src = open(os.path.join(here, "backtester.py")).read()
check("metrics() records pnl_sd", '"pnl_sd"' in bt_src)

# --- WIRED into the live path, and failing CLOSED -----------------------
# v59.0: this assertion used to require the OPPOSITE — that the gate was
# absent from the live path. That was correct while item 17 was still a
# report. The call was made on 2026-08-01: auto-promotion on a sign test
# stops, the strategies keep running in paper.
check("wired into is_live_enabled", "gate_verdict" in bt_src and
      "def is_live_enabled" in bt_src)
_body = bt_src.split("def is_live_enabled")[1].split("\ndef ")[0]
check("is_live_enabled consults the gate", "gate_verdict" in _body)
check("a broken gate denies live rather than allowing it",
      "return False" in _body.split("except Exception")[1],
      "failing open would silently restore the sign test")
# Paper must be untouched. The guarantee is not in is_live_enabled at all
# — it is that every CALL SITE is already behind `not paper_mode`, so a
# gate that denies live cannot stop a paper trade. Assert that where it
# actually lives, in agents.py, rather than on this function's text.
ag = open(os.path.join(here, "agents.py")).read()
_sites = [ln for ln in ag.splitlines() if "backtester.is_live_enabled(" in ln]
check("every is_live_enabled call site is behind paper_mode",
      _sites and all("paper_mode" in ln for ln in _sites),
      f"{len(_sites)} call sites; all eleven strategies keep running in paper")

import backtester as bt
ok3, det = bt.gate_verdict("no_such_strategy", "NIFTY")
check("unknown strategy is denied, not crashed", ok3 is False, str(det))
check("is_live_enabled is False for an unknown strategy",
      bt.is_live_enabled("no_such_strategy", "NIFTY") is False)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all promotion-gate checks passed")
