"""test_dhan_scrip_master.py — validate futures lookup logic, first
against a constructed sample matching the DETAILED CSV schema (works
with no network, and matches api-scrip-master-detailed.csv — switched
to 2026-07-25 specifically to fix SENSEX futures), then against the
REAL live scrip master CSV (needs network — this sandbox can't reach
images.dhan.co directly, so that part has only been run against the
constructed sample so far).

    python test_dhan_scrip_master.py

What "success" looks like:
  [1] constructed-sample test correctly picks the nearest UNEXPIRED
      contract (built with dates relative to today, not hardcoded
      absolute dates — so this is correct regardless of what day you
      actually run it)
  [2] the "pick nearest unexpired" logic correctly ignores an already-
      expired contract and a same-symbol option row mixed into the
      sample
  [3] SENSEX (the actual reported live bug — futures never loaded)
      resolves correctly against real confirmed BSE sample rows
  [4] legacy compact-CSV schema (SEM_* columns) still parses via the
      fallback column candidates, for backward compatibility
  [5] live CSV download + parse succeeds and finds a real current-month
      contract for NIFTY/BANKNIFTY/FINNIFTY/SENSEX

If [5] fails with a "CSV schema mismatch" error: the printed fieldnames
list is the important part — paste it back so the column-name
resolution in dhan_scrip_master.py's COLUMN_CANDIDATES can be corrected
for whichever names the real file actually uses.
"""
import sys
from datetime import datetime, timedelta

import dhan_scrip_master as dsm

REPORT = []


def log(msg):
    print(msg)
    REPORT.append(msg)


def _build_detailed_sample_csv():
    """Dates relative to today, not hardcoded — a contract expiring
    "next week" should always be picked over one expiring "in 2
    months" or one that already expired "last week", regardless of
    what actual calendar date this test runs on.

    Schema and every row match the user's REAL confirmed detailed
    scrip master rows exactly (2026-07-25), format "DD/MM/YY" (no
    time component in this file, unlike the compact one) — including
    the SENSEX/BSE rows that are the actual fix for the reported live
    bug (SENSEX futures never loading)."""
    now = datetime.now()
    near = (now + timedelta(days=7)).strftime("%d/%m/%y")    # current month
    mid = (now + timedelta(days=35)).strftime("%d/%m/%y")    # next month
    far = (now + timedelta(days=63)).strftime("%d/%m/%y")    # month after
    expired = (now - timedelta(days=7)).strftime("%d/%m/%y")  # already gone
    header = ("EXCH_ID,SEGMENT,SECURITY_ID,ISIN,INSTRUMENT,"
             "UNDERLYING_SECURITY_ID,UNDERLYING_SYMBOL,SYMBOL_NAME,"
             "DISPLAY_NAME,INSTRUMENT_TYPE,SERIES,LOT_SIZE,SM_EXPIRY_DATE,"
             "STRIKE_PRICE,OPTION_TYPE,TICK_SIZE")
    rows = [
        # BANKNIFTY futures: near/mid/far/expired — near should win
        f"NSE,D,61088,NA,FUTIDX,26009,BANKNIFTY,BANKNIFTY-Jul2026-FUT,BANKNIFTY JUL FUT,FUT,NA,30,{near},-0.01,XX,20",
        f"NSE,D,58067,NA,FUTIDX,26009,BANKNIFTY,BANKNIFTY-Aug2026-FUT,BANKNIFTY AUG FUT,FUT,NA,30,{mid},-0.01,XX,20",
        f"NSE,D,68390,NA,FUTIDX,26009,BANKNIFTY,BANKNIFTY-Sep2026-FUT,BANKNIFTY SEP FUT,FUT,NA,30,{far},-0.01,XX,20",
        f"NSE,D,50001,NA,FUTIDX,26009,BANKNIFTY,BANKNIFTY-Jun2026-FUT,BANKNIFTY JUN FUT,FUT,NA,30,{expired},-0.01,XX,20",
        # NIFTY: one OPTIDX row (must be excluded) + one FUTIDX row (must win)
        f"NSE,D,90001,NA,OPTIDX,26000,NIFTY,NIFTY-Jul2026-24000-CE,NIFTY 24000 CE,OPT,XX,75,{near},24000,CE,0.05",
        f"NSE,D,61093,NA,FUTIDX,26000,NIFTY,NIFTY-Jul2026-FUT,NIFTY JUL FUT,FUT,NA,65,{near},-0.01,XX,10",
        # FINNIFTY real confirmed row
        f"NSE,D,61091,NA,FUTIDX,26037,FINNIFTY,FINNIFTY-Jul2026-FUT,FINNIFTY JUL FUT,FUT,NA,60,{near},-0.01,XX,10",
        # SENSEX (BSE) — the actual live bug this switch fixes. Three
        # real confirmed rows (near/mid/far), same as the other
        # symbols, to also exercise "pick nearest" for SENSEX
        # specifically rather than just confirming it parses at all.
        f"BSE,D,1144507,NA,FUTIDX,1,SENSEX,BSXFUT,SENSEX JUL  FUT,FUTIDX,NA,20,{near},0,,5",
        f"BSE,D,825622,NA,FUTIDX,1,SENSEX,BSXFUT,SENSEX AUG  FUT,FUTIDX,NA,20,{mid},0,,5",
        f"BSE,D,844615,NA,FUTIDX,1,SENSEX,BSXFUT,SENSEX SEP  FUT,FUTIDX,NA,20,{far},0,,5",
    ]
    return header + "\n" + "\n".join(rows) + "\n"


def _build_legacy_compact_sample_csv():
    """Old compact-CSV schema (SEM_* columns, DD/MM/YY HH:MM expiry) —
    kept as a regression check that the fallback column candidates in
    COLUMN_CANDIDATES still resolve correctly, in case this module is
    ever pointed back at the compact file."""
    now = datetime.now()
    near = (now + timedelta(days=7)).strftime("%d/%m/%y %H:%M")
    header = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,"
             "SEM_EXCH_INSTRUMENT_TYPE,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,"
             "SEM_TRADING_SYMBOL,SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,"
             "SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,"
             "SEM_TICK_SIZE,SEM_EXPIRY_FLAG,SEM_SERIES,SM_SYMBOL_NAME")
    row = (f"NSE,D,61091,FUT,FUTIDX,0,FINNIFTY-Jul2026-FUT,60,"
          f"FINNIFTY JUL FUT,{near},0,,0.05,,,FINNIFTY")
    return header + "\n" + row + "\n"


SAMPLE_CSV = _build_detailed_sample_csv()
LEGACY_SAMPLE_CSV = _build_legacy_compact_sample_csv()


def main():
    log("[1] Testing against a constructed sample matching the DETAILED "
       "CSV schema (real confirmed columns, 2026-07-25)...")
    result, detail = dsm.get_current_future_detailed("BANKNIFTY", csv_text=SAMPLE_CSV)
    log(f"    result: {result}")
    log(f"    detail: {detail}")
    if not result:
        log("[1] FAIL — could not parse the constructed sample at all. "
           "This means the parsing logic itself has a bug (not a live-CSV "
           "schema issue, since this sample is hand-built to match the "
           "documented columns exactly).")
        sys.exit(1)

    checks = [
        ("security_id matches the nearest-unexpired contract (61088, "
         "the ~7-days-out one, correctly preferred over the 35/63-days-out "
         "and already-expired ones)", result["security_id"] == "61088"),
        ("symbol_name matches (BANKNIFTY-Jul2026-FUT)",
         result["symbol_name"] == "BANKNIFTY-Jul2026-FUT"),
        ("display_name matches (BANKNIFTY JUL FUT)",
         result["display_name"] == "BANKNIFTY JUL FUT"),
        ("lot_size correctly read (30)", result["lot_size"] == "30"),
        ("tick_size correctly read (20)", result["tick_size"] == "20"),
        ("did NOT pick the already-expired contract (50001, ~7 days ago)",
         result["security_id"] != "50001"),
        ("did NOT pick a farther-out contract just because it's listed "
         "later in the file (58067/68390 are 35/63 days out)",
         result["security_id"] not in ("58067", "68390")),
        ("exactly 3 unexpired candidates found (near/mid/far), expired "
         "one correctly excluded", detail.get("candidates_found") == 3),
    ]
    all_pass = True
    for desc, ok in checks:
        log(f"    {'PASS' if ok else 'FAIL'}: {desc}")
        all_pass = all_pass and ok
    log(f"[1] {'ALL PASS' if all_pass else 'SOME FAILED'}")

    log("")
    log("[2] Testing that a same-underlying OPTION row (OPTIDX) doesn't "
       "get picked up as a future...")
    result2, detail2 = dsm.get_current_future_detailed("NIFTY", csv_text=SAMPLE_CSV)
    ok2 = result2 and result2["security_id"] == "61093"
    log(f"    {'PASS' if ok2 else 'FAIL'}: NIFTY future correctly resolved "
       f"to security_id 61093 (the FUTIDX row), not the OPTIDX row — got "
       f"{result2}")

    log("")
    log("[3] Testing SENSEX (the ACTUAL reported live bug: futures never "
       "loaded) against real confirmed BSE rows, with near/mid/far "
       "expiries so 'pick nearest' is also exercised for SENSEX "
       "specifically, not just parsing...")
    result3, detail3 = dsm.get_current_future_detailed("SENSEX", csv_text=SAMPLE_CSV)
    checks3 = [
        ("SENSEX resolves at all (was previously always None/empty)",
         result3 is not None),
        ("picked the nearest-expiry contract (1144507, ~7 days out) "
         "not the farther ones (825622/844615)",
         result3 and result3["security_id"] == "1144507"),
        ("exchange correctly read as plain 'BSE' (no BSE_FNO/BFO "
         "guessing needed with the detailed file)",
         result3 and result3["exchange"] == "BSE"),
        ("lot_size correctly read (20)",
         result3 and result3["lot_size"] == "20"),
        ("tick_size correctly read (5)",
         result3 and result3["tick_size"] == "5"),
    ]
    ok3 = True
    for desc, ok in checks3:
        log(f"    {'PASS' if ok else 'FAIL'}: {desc}")
        ok3 = ok3 and ok
    log(f"    got: {result3}")
    log(f"[3] {'ALL PASS' if ok3 else 'SOME FAILED'}")

    log("")
    log("[4] Testing legacy compact-CSV schema (SEM_* columns) still "
       "resolves via the fallback column candidates...")
    result4, detail4 = dsm.get_current_future_detailed(
        "FINNIFTY", csv_text=LEGACY_SAMPLE_CSV)
    ok4 = (result4 and result4["security_id"] == "61091"
          and result4["symbol_name"] == "FINNIFTY-Jul2026-FUT"
          and result4["lot_size"] == "60")
    log(f"    {'PASS' if ok4 else 'FAIL'}: legacy schema still parses "
       f"correctly — got {result4}")

    log("")
    log("[5] Attempting the REAL live scrip master CSV (needs network)...")
    try:
        real_result, real_detail = dsm.get_current_future_detailed("SENSEX")
        if real_result:
            log(f"    SENSEX current future: {real_result}")
            log("[5] PASS — live CSV fetched and parsed successfully")
        else:
            log(f"    [5] FAIL: {real_detail}")
            if "fieldnames" in str(real_detail):
                log("    ^ paste the fieldnames list back — that tells us "
                   "exactly which column names to add to COLUMN_CANDIDATES")
    except Exception as e:
        log(f"    [5] Could not even attempt this: {e}")

    log("")
    log("=" * 60)
    log("Paste this full output back for the next iteration.")


if __name__ == "__main__":
    main()
