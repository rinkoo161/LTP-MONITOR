"""backtest_s10.py — replay Strategy 10 over the archived chain.

    python3 backtest_s10.py --symbol NIFTY --days 5

WHAT THIS CAN AND CANNOT TELL YOU
---------------------------------
The option half of the trigger is fully replayable: chain_snapshots
stores per-strike ltp/oi/oi_chg/volume/delta every 60 seconds and keeps
5 days, and `chg` is derived from consecutive snapshots.

The futures half was NOT archived before v58.66. So on older days the
run falls back to mode="chain_only", which ASSUMES the futures condition
agreed — that OVERSTATES the trigger count and is not a performance
estimate. It is still useful: it tells you how often the chain condition
occurs and lets you eyeball whether the setups are ones you would take.

From v58.66 onward futures OI is recorded, so a run a few days from now
will report mode="full" and mean what it looks like it means.
"""
import argparse
import datetime as dt
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="NIFTY")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--capital", type=float, default=1000000)
    ap.add_argument("--show", type=int, default=8, help="setups to print")
    a = ap.parse_args()

    import history
    import oi_composite as oc

    today = dt.date.today()
    total = 0
    for i in range(a.days):
        day = (today - dt.timedelta(days=i)).strftime("%Y-%m-%d")
        fut = history.future_oi_series(a.symbol, day)
        r = oc.replay(a.symbol, day, a.capital, futures_series=fut or None)
        if r["mode"] == "no_data":
            print(f"{day}  {a.symbol:10s} — no archived chain")
            continue
        total += r["n_setups"]
        print(f"\n{day}  {a.symbol:10s} mode={r['mode']}  "
              f"snapshots={r['n_snapshots']}  setups={r['n_setups']}  "
              f"({r['setups_per_hour']}/hr)")
        if r["by_kind"]:
            print("   by kind:", r["by_kind"])
        for s in r["setups"][:a.show]:
            t = dt.datetime.fromtimestamp(s["ts"]).strftime("%H:%M")
            tag = s.get("future_quadrant") or f"assumed {s.get('assumed_future')}"
            print(f"   {t}  {s['kind']:20s} [{tag}]  {str(s['why'])[:88]}")
        if r["mode"] == "chain_only":
            print(f"   NOTE {r['caveat']}")
    print(f"\ntotal setups across {a.days} day(s): {total}")
    if total == 0:
        print("Zero setups is itself a result: either the archive is empty, or "
              "the OI conditions genuinely did not co-occur. Check "
              "n_snapshots above to tell which.")


if __name__ == "__main__":
    main()
