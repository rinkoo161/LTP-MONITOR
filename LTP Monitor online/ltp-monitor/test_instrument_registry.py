#!/usr/bin/env python3
"""test_instrument_registry.py — Phase 1 of the stock-options work.

DATA ONLY. Nothing here, and nothing in the module under test, places an
order or enables a strategy. The registry answers: given a symbol the
user typed, what does the system need to know, and is it tradable at all.

Driven against a STUB scrip master, not the live 34 MB CSV, so the test
is deterministic and offline. The shapes below are copied from real rows
observed on 2026-08-04 — including the ADANIPORTS BSE row whose
SYMBOL_NAME is 'MPSLFUT', unrelated to its underlying, which is why
matching is on UNDERLYING_SYMBOL rather than SYMBOL_NAME.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_instrument_registry")

import instrument_registry as ir

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


def row(exch, inst, sid, under, name, lot):
    return {"EXCH_ID": exch, "INSTRUMENT": inst, "SECURITY_ID": sid,
            "UNDERLYING_SYMBOL": under, "SYMBOL_NAME": name, "LOT_SIZE": lot}


STUB = (
    [row("NSE", "OPTSTK", str(900 + i), "ADANIENSOL", "ADANIENSOL-OPT", "675.0")
     for i in range(316)]
    + [row("NSE", "FUTSTK", "58087", "ADANIENSOL", "ADANIENSOL-Aug2026-FUT", "675.0"),
       row("NSE", "FUTSTK", "68416", "ADANIENSOL", "ADANIENSOL-Sep2026-FUT", "675.0"),
       row("NSE", "EQUITY", "10217", "ADANIENSOL", "ADANI ENERGY SOLUTION LTD", "1.0")]
    # An INDEX, to prove the same path serves both kinds.
    + [row("NSE", "OPTIDX", str(700 + i), "NIFTY", "NIFTY-OPT", "65.0")
       for i in range(20)]
    + [row("NSE", "INDEX", "13", "NIFTY", "NIFTY 50", "0")]
    # The dual-listing trap, verbatim in shape: BSE futures whose
    # SYMBOL_NAME has nothing to do with the underlying.
    + [row("BSE", "FUTSTK", "826495", "ADANIPORTS", "MPSLFUT", "475.0"),
       row("BSE", "OPTSTK", "826496", "ADANIPORTS", "MPSLOPT", "475.0"),
       row("BSE", "EQUITY", "532921", "ADANIPORTS", "ADANI PORTS AND SPECIAL E", "1.0"),
       row("NSE", "OPTSTK", "999", "ADANIPORTS", "ADANIPORTS-OPT", "475.0"),
       row("NSE", "EQUITY", "15083", "ADANIPORTS", "ADANI PORT & SEZ LTD", "1.0")]
    # An underlying that exists but has NO options — a different failure
    # from "no such name", and the user needs to be told which.
    + [row("NSE", "EQUITY", "4321", "SOMECASHONLY", "SOME CASH ONLY LTD", "1.0")]
)
ir._CACHE["rows"] = STUB

print("1) the target resolves, with everything the API needs")
ok, why, d = ir.validate("ADANIENSOL")
check("ADANIENSOL validates", ok, why)
check("underlying id is the EQUITY row, not an option row",
      d and d["underlying_id"] == "10217", str(d and d["underlying_id"]))
check("lot size is READ from the CSV", d and d["lot_size"] == 675,
      "stock F&O lots are revised often; a hardcoded table is wrong by "
      "construction")
check("kind is stock", d and d["kind"] == "stock")
check("contracts are counted", d and d["n_option_rows"] == 316
      and d["n_future_rows"] == 2, str(d))

print("\n2) INDEX vs STOCK differ in the segment the chain call needs")
di = ir.resolve("NIFTY")
check("an index resolves through the same path", di is not None)
check("index UnderlyingSeg is IDX_I", di and di["underlying_seg"] == "IDX_I",
      "returning NSE_FNO here compiles and then fails at the API — the "
      "bug this check exists for")
check("stock UnderlyingSeg is the F&O segment",
      d and d["underlying_seg"] == "NSE_FNO", str(d and d["underlying_seg"]))
check("index lot size also comes from the CSV", di and di["lot_size"] == 65,
      "matches config.lot_sizes for NIFTY — a cross-check that the CSV "
      "reading is right")

print("\n3) the dual-listing trap")
# ADANIPORTS BSE rows carry SYMBOL_NAME 'MPSLFUT'. Matching on that name
# resolves the wrong contract silently.
nse = ir.resolve("ADANIPORTS", "NSE")
bse = ir.resolve("ADANIPORTS", "BSE")
check("NSE and BSE resolve separately", nse and bse)
check("and to DIFFERENT underlying ids",
      nse["underlying_id"] == "15083" and bse["underlying_id"] == "532921",
      f"{nse['underlying_id']} vs {bse['underlying_id']}")
check("the BSE row is found by UNDERLYING_SYMBOL, not SYMBOL_NAME",
      bse["n_option_rows"] == 1,
      "'MPSLOPT' shares no substring with ADANIPORTS")
check("BSE maps to the BSE F&O segment",
      bse["fno_segment"] == "BSE_FNO", bse["fno_segment"])

print("\n4) rejection says WHY, because the user has to act on it")
ok2, why2, _ = ir.validate("SOMECASHONLY")
check("an options-less underlying is rejected", not ok2)
check("and is distinguished from a typo",
      "no options" in why2, why2)
ok3, why3, _ = ir.validate("ADANI ENERGY")
check("a name not in the master is rejected", not ok3)
check("with a spelling hint", "scrip master" in why3, why3)
ok4, why4, _ = ir.validate("")
check("empty is rejected", not ok4 and "empty" in why4)

print("\n5) the picker only offers what can actually be analysed")
res = ir.search("ADANI")
names = {(r["symbol"], r["exchange"]) for r in res}
check("option-bearing underlyings are offered",
      ("ADANIENSOL", "NSE") in names and ("ADANIPORTS", "NSE") in names,
      str(sorted(names)))
check("the cash-only name is NOT offered",
      not any(r["symbol"] == "SOMECASHONLY" for r in res),
      "offering a name the system cannot analyse is a support question, "
      "not a feature")
check("results are ordered by depth",
      [r["option_rows"] for r in res] == sorted(
          [r["option_rows"] for r in res], reverse=True), str(res[:3]))

print("\n6) it is DATA ONLY — no order path, no strategy enablement")
SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "instrument_registry.py")).read()
for forbidden in ("enter_", "place_order", "manual_trade", "auto_deploy",
                  "_enabled"):
    check(f"no reference to {forbidden}", forbidden not in SRC,
          "Phase 1 archives and measures; trading is a later decision")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all instrument-registry checks passed")
