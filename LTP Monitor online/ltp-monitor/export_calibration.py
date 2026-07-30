#!/usr/bin/env python3
"""export_calibration.py — bundle Strategy 9's calibration data into ONE
file that can be shared for analysis.

Why a script rather than "just paste the endpoint output": the summary
endpoint answers *whether* a signal fires, but not *when* or *alongside
what*. If `gmma_expansion` never fires, the fix depends on whether the
ribbon was interleaved the whole session (threshold too tight) or
separated but never widening (the wrong test entirely) — and that needs
the raw per-candle rows, not the aggregate. This writes both.

Usage, from the project directory with the app STOPPED or running
(reads are safe either way, the store is WAL-mode):

    python3 export_calibration.py                # last 5 days, all symbols
    python3 export_calibration.py --days 1       # just today
    python3 export_calibration.py --symbol NIFTY

Writes ta_calibration_export_<date>.json next to this script and prints
the path. That single file is what to share.

Contains NO credentials, order history, P&L or position data — only
indicator readings and their timestamps. Worth stating plainly, since
this file is meant to leave the machine.
"""
import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import history  # noqa: E402

COLUMNS = ["ts", "as_of", "day", "symbol", "strategy", "phase", "route",
           "tide", "direction", "bb_state", "gmma_state", "adx", "dynamic",
           "macd_zero_reversal", "rsi", "sig_bb_stall", "sig_gmma",
           "sig_macd_zero", "sig_hidden_div", "sig_regular_div",
           "sig_rsi_div", "sig_adx", "confluence_hits", "confluence_need",
           "fired", "blocked"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--symbol", default=None)
    ap.add_argument("--max-rows", type=int, default=4000,
                    help="cap on raw rows so the file stays shareable")
    args = ap.parse_args()

    import time
    cutoff = int(time.time()) - args.days * 86400
    c = history._conn()
    where = "WHERE ts >= ?" + (" AND symbol = ?" if args.symbol else "")
    params = (cutoff,) + ((args.symbol,) if args.symbol else ())
    rows = c.execute(
        f"SELECT {','.join(COLUMNS)} FROM ta_calibration {where} "
        f"ORDER BY as_of DESC LIMIT ?", params + (args.max_rows,)).fetchall()
    c.close()

    summary = history.ta_calibration_summary(days=args.days,
                                             symbol=args.symbol)
    # Per-symbol breakdown too: one index behaving differently from the
    # others is itself a finding, and the all-symbol aggregate hides it.
    per_symbol = {}
    for sym in sorted({r[COLUMNS.index("symbol")] for r in rows}):
        per_symbol[sym] = history.ta_calibration_summary(days=args.days,
                                                         symbol=sym)

    import config
    cfg = config.load()
    payload = {
        "exported_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "app_version": open(os.path.join(os.path.dirname(__file__),
                                         "VERSION")).read().strip(),
        "window_days": args.days,
        "symbol_filter": args.symbol,
        # The thresholds in force WHEN the data was collected. Without
        # these the rows cannot be interpreted - a 0% hit rate means
        # nothing if the threshold that produced it is unknown.
        "settings_in_force": {k: cfg.get(k) for k in sorted(cfg)
                              if k.startswith("ta_") or k.startswith("s8_")},
        "summary_all_symbols": summary,
        "summary_per_symbol": per_symbol,
        "row_count": len(rows),
        "columns": COLUMNS,
        "rows": [list(r) for r in rows],
    }

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"ta_calibration_export_"
                       f"{datetime.date.today().isoformat()}.json")
    with open(out, "w") as f:
        json.dump(payload, f, indent=1, default=str)

    size_kb = os.path.getsize(out) / 1024
    print(f"\n  wrote {out}  ({size_kb:.0f} KB, {len(rows)} rows)")
    if not rows:
        print("\n  NO ROWS. Checklist:")
        print("    - was the app running during market hours?")
        print("    - is ta_elliott_enabled on?  (ta_auto_deploy can stay off)")
        print("    - is ta_calibration_logging on?")
        print("    - check the Agents page: the ta_elliott row should show")
        print("      phases[...] confluence[...] in its summary")
    else:
        s = summary
        print(f"\n  {s['observations']} observations")
        print(f"  phase mix: {s['phase_pct']}")
        print(f"  signals never firing: {s['signals_never_firing'] or 'none'}")
        print(f"  confluence distribution: {s['confluence_distribution']}")
    print("\n  Share that one file.\n")


if __name__ == "__main__":
    main()
