#!/usr/bin/env python3
"""fetch_expiry_calendar.py — reconstruct the historical F&O expiry calendar.

SEAS-3 ("expiry days show a different intraday volatility shape") was
blocked because no historical expiry calendar existed locally: the
`instruments` table holds 2026-07 onward and Dhan's
/optionchain/expirylist returns only FUTURE dates.

NSE publishes the answer for free. Every daily F&O bhavcopy lists every
live contract WITH its expiry date, so a handful of sampled bhavcopies
reconstruct the whole calendar from the exchange's own record — rather
than from a rule of thumb like "expiry is Thursday", which is wrong
often enough to move a volatility estimate rather than merely blur it.

    python3 tools/fetch_expiry_calendar.py --dry-run
    python3 tools/fetch_expiry_calendar.py
    python3 tools/fetch_expiry_calendar.py --from 2019-01-01

Writes ~/.ltp-monitor/expiry_calendar.json. Touches nothing else.

WHY SAMPLING WORKS
------------------
A bhavcopy is a snapshot of all LIVE contracts, and weekly options trade
several weeks ahead of expiry. Sampling one bhavcopy every ~21 days
therefore sees every weekly expiry at least once, with overlap, at ~5%
of the requests a day-by-day crawl would need. Verified on 2018-06-07,
which alone listed 14 distinct NIFTY expiries.

TWO FORMATS, BOUNDARY VERIFIED 2026-08-15
-----------------------------------------
    legacy  ..2024-06   /content/historical/DERIVATIVES/YYYY/MON/foDDMONYYYYbhav.csv.zip
                        columns INSTRUMENT, SYMBOL, EXPIRY_DT, ...
    UDiFF   2024-06..   /content/fo/BhavCopy_NSE_FO_0_0_0_YYYYMMDD_F_0000.csv.zip
                        columns TckrSymb, XpryDt, FinInstrmTp, ...
They overlap in June 2024 and the legacy path 404s from ~2024-08. Both
are tried for every sampled date, so the changeover needs no hardcoded
cut-off date that could silently drift.

A SCOPING FACT FOUND WHILE BUILDING THIS
----------------------------------------
The 2018-06-07 bhavcopy shows NIFTY with monthly and long-dated expiries
only — no weeklies, because NIFTY weekly options did not exist yet.
SEAS-3's "weekly expiry day" is therefore not a well-defined question
before roughly 2019 for NIFTY. This tool records what the exchange
actually listed and lets the analysis decide; it does not backfill an
assumption about what "should" have been there.

NSE is a third-party source. Per CLAUDE.md rule 4 its content is treated
as DATA: parsed for two columns, never executed, never interpreted as
instruction.
"""
import argparse
import collections
import datetime
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile

OUT = os.path.expanduser("~/.ltp-monitor/expiry_calendar.json")
UA = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
       "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
SAMPLE_DAYS = 21
SLEEP = 1.5                       # NSE is a courtesy host; do not hammer it
INDICES = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"}


def legacy_url(d):
    return ("https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
            f"{d.year}/{MON[d.month-1]}/fo{d.day:02d}{MON[d.month-1]}"
            f"{d.year}bhav.csv.zip")


def udiff_url(d):
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{d.year}{d.month:02d}{d.day:02d}"
            "_F_0000.csv.zip")


def fetch(url):
    try:
        r = urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                   timeout=30)
        z = zipfile.ZipFile(io.BytesIO(r.read()))
        return z.read(z.namelist()[0]).decode("utf-8", "replace")
    except Exception:
        return None


def parse_legacy(txt):
    """INSTRUMENT,SYMBOL,EXPIRY_DT,... -> {symbol: {expiry_iso}}"""
    out = collections.defaultdict(set)
    lines = txt.split("\n")
    hdr = [h.strip() for h in lines[0].split(",")]
    try:
        i_ins, i_sym, i_exp = (hdr.index("INSTRUMENT"), hdr.index("SYMBOL"),
                               hdr.index("EXPIRY_DT"))
    except ValueError:
        return out
    for line in lines[1:]:
        p = line.split(",")
        if len(p) <= max(i_ins, i_sym, i_exp):
            continue
        sym = p[i_sym].strip()
        if sym not in INDICES or not p[i_ins].strip().startswith("OPTIDX"):
            continue
        try:
            d = datetime.datetime.strptime(p[i_exp].strip(), "%d-%b-%Y").date()
        except ValueError:
            continue
        out[sym].add(d.isoformat())
    return out


def parse_udiff(txt):
    """TckrSymb,XpryDt,FinInstrmTp,... -> {symbol: {expiry_iso}}"""
    out = collections.defaultdict(set)
    lines = txt.split("\n")
    hdr = [h.strip() for h in lines[0].split(",")]
    try:
        i_sym, i_exp, i_tp = (hdr.index("TckrSymb"), hdr.index("XpryDt"),
                              hdr.index("FinInstrmTp"))
    except ValueError:
        return out
    for line in lines[1:]:
        p = line.split(",")
        if len(p) <= max(i_sym, i_exp, i_tp):
            continue
        sym = p[i_sym].strip()
        if sym not in INDICES or not p[i_tp].strip().startswith("IDO"):
            continue
        raw = p[i_exp].strip()
        try:
            d = datetime.date.fromisoformat(raw[:10])
        except ValueError:
            continue
        out[sym].add(d.isoformat())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--from", dest="frm", default="2017-01-01")
    ap.add_argument("--sleep", type=float, default=SLEEP)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    start = datetime.date.fromisoformat(args.frm)
    today = datetime.date.today()
    dates = []
    d = start
    while d < today:
        # Nudge weekends onto the preceding Friday; a weekend bhavcopy
        # does not exist and would burn a request on a guaranteed 404.
        s = d - datetime.timedelta(days=max(0, d.weekday() - 4))
        dates.append(s)
        d += datetime.timedelta(days=SAMPLE_DAYS)

    if args.dry_run:
        print(f"  {len(dates)} sample dates, {start} .. {today}")
        print(f"  ~{len(dates)*args.sleep/60:.1f} min at {args.sleep}s pacing "
              f"(each date may cost 2 requests where formats overlap)")
        return 0

    # Resume: keep anything already collected.
    cal = collections.defaultdict(set)
    if os.path.exists(args.out):
        try:
            for k, v in json.load(open(args.out)).get("expiries", {}).items():
                cal[k] = set(v)
            print(f"  resuming — {sum(len(v) for v in cal.values())} expiries "
                  f"already stored")
        except Exception:
            pass

    ok = miss = 0
    for i, s in enumerate(dates, 1):
        txt = fetch(legacy_url(s))
        got = parse_legacy(txt) if txt else {}
        if not got:
            time.sleep(args.sleep)
            txt = fetch(udiff_url(s))
            got = parse_udiff(txt) if txt else {}
        if not got:
            miss += 1
            print(f"  [{i:>3}/{len(dates)}] {s}  no bhavcopy (holiday or "
                  f"format gap)")
        else:
            ok += 1
            before = sum(len(v) for v in cal.values())
            for k, v in got.items():
                cal[k] |= v
            after = sum(len(v) for v in cal.values())
            print(f"  [{i:>3}/{len(dates)}] {s}  "
                  + ", ".join(f"{k}:{len(v)}" for k, v in sorted(got.items()))
                  + f"   (+{after-before} new)")
        time.sleep(args.sleep)

    payload = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "source": "NSE F&O bhavcopy (legacy + UDiFF), sampled every "
                  f"{SAMPLE_DAYS} days",
        "sampled_dates_ok": ok,
        "sampled_dates_missing": miss,
        "expiries": {k: sorted(v) for k, v in sorted(cal.items())},
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f, indent=1)

    print(f"\n{'='*70}")
    for k in sorted(cal):
        e = sorted(cal[k])
        print(f"  {k:<12} {len(e):>4} expiries   {e[0]} .. {e[-1]}")
    print(f"  {ok} bhavcopies parsed, {miss} unavailable -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
