#!/usr/bin/env python3
"""test_futures_candle_archive.py — roadmap B9.

From 2026-08-03 the INDEX stops being discovered at 15:15: every
NIFTY/BANKNIFTY/FINNIFTY constituent is an F&O stock, so they all enter
the closing call auction and the index repeats its last value until the
official close arrives as one step. Futures trade on to 15:40.

Measured on the real 2026-08-04 archive, same window, same day:

    INDEX    15:14-15:40   25 bars, 23 FLAT, largest 1-bar move 151.5
    FUTURES  15:14-15:40   26 bars,  0 FLAT, largest 1-bar move  11.9

So futures are the only instrument with real prices in that window — and
until now the system archived futures OI (`future_oi_snapshots`) and no
futures PRICE SERIES at all. "Are futures actually unfrozen?" could only
be answered from OI-snapshot LTPs at whatever cadence the agent happened
to run, which on 2026-08-04 was four samples because the machine slept.

The archiver is driven with a STUB broker here — no network — because
the point under test is the wiring (segment, instrument type, resolver
reuse, where rows land), not Dhan's uptime.
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_futures_candle_archive")

import history

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
DAY = "2026-08-04"


class StubDhan:
    """Records how it was called; returns bars that MOVE, which is the
    property that distinguishes futures from the frozen index."""
    def __init__(self):
        self.calls = []

    def _intraday_range_sid(self, sid, interval, frm, to, segment=None,
                            instrument=None):
        self.calls.append({"sid": str(sid), "interval": interval,
                           "segment": segment, "instrument": instrument})
        base = datetime.datetime(2026, 8, 4, 15, 20, tzinfo=IST)
        return [{"ts": int((base + datetime.timedelta(minutes=i)).timestamp()),
                 "o": 24550 + i, "h": 24555 + i, "l": 24548 + i,
                 "c": 24552 + i, "v": 100 + i} for i in range(10)]


_real_sm = None
try:
    import dhan_scrip_master as _sm
    _real_sm = _sm.get_current_futures_for_symbols
    _sm.get_current_futures_for_symbols = lambda syms, n=3: {
        s: ([{"security_id": "58072", "symbol_name": "NIFTY-Aug2026-FUT",
              "expiry": datetime.datetime(2026, 8, 25, tzinfo=IST)},
             {"security_id": "68407", "symbol_name": "NIFTY-Sep2026-FUT",
              "expiry": datetime.datetime(2026, 9, 29, tzinfo=IST)}][:n], {})
        for s in syms}
except Exception as e:                                    # pragma: no cover
    print(f"  cannot stub the resolver: {e}")

print("1) it archives, and the rows land under the FUTURES security_id")
stub = StubDhan()
written = history.sync_futures_candles(stub, "NIFTY", DAY,
                                       log=lambda m: None, n=2)
check("candles were written", written > 0, f"{written} rows")
conn = history._conn()
n_front = conn.execute("SELECT COUNT(*) FROM candles WHERE security_id='58072'"
                       ).fetchone()[0]
n_m2 = conn.execute("SELECT COUNT(*) FROM candles WHERE security_id='68407'"
                    ).fetchone()[0]
check("front month has rows", n_front > 0, str(n_front))
check("month2 is archived too", n_m2 > 0,
      "cross-month OI/volume work needs the far months to have lead time")

print("\n2) the contract is registered as a FUTURE, with its expiry")
row = conn.execute("SELECT symbol,kind,expiry FROM instruments "
                   "WHERE security_id='58072'").fetchone()
conn.close()
check("registered", row is not None)
check("symbol and kind are right", row and row[0] == "NIFTY" and row[1] == "fut",
      str(row))
check("expiry is recorded", row and row[2] == "2026-08-25", str(row and row[2]))

print("\n3) it asks the broker for the right thing")
check("interval is 1m", all(c["interval"] == "1" for c in stub.calls),
      str([c["interval"] for c in stub.calls]))
check("instrument type is FUTIDX, not OPTIDX",
      all(c["instrument"] == "FUTIDX" for c in stub.calls),
      str([c["instrument"] for c in stub.calls]))
check("segment is NSE_FNO for NIFTY",
      all(c["segment"] == "NSE_FNO" for c in stub.calls),
      str([c["segment"] for c in stub.calls]))

print("\n4) SENSEX goes to BSE, not NSE — the one that differs")
stub2 = StubDhan()
history.sync_futures_candles(stub2, "SENSEX", DAY, log=lambda m: None, n=1)
check("SENSEX uses BSE_FNO",
      stub2.calls and stub2.calls[0]["segment"] == "BSE_FNO",
      str([c["segment"] for c in stub2.calls]))

print("\n5) it reuses the SHARED contract resolver")
HERE = os.path.dirname(os.path.abspath(__file__))
HSRC = open(os.path.join(HERE, "history.py")).read()
_body = HSRC.split("def sync_futures_candles")[1]
_body = _body[:_body.index("\ndef ")] if "\ndef " in _body else _body
check("it calls get_current_futures_for_symbols",
      "get_current_futures_for_symbols" in _body,
      "a second contract resolver would drift from MarketDataAgent's — "
      "the failure this codebase has already had three times")
check("it paces the rate-limited endpoint", "time.sleep" in _body,
      "same endpoint as the option-leg archive, with a documented 429 history")

print("\n6) it is wired into the daily archive, beside the option legs")
AG = open(os.path.join(HERE, "agents.py")).read()
check("the daily sync calls it", "history.sync_futures_candles(" in AG)
_seg = AG.split("history.sync_day_chain(")[1][:900]
check("and it runs in the same loop as sync_day_chain",
      "sync_futures_candles" in _seg,
      "one driver, so futures cannot silently stop being archived while "
      "options continue")

print("\n6b) STOCK futures — the two bugs a stock archive exposed")
# Both were invisible while only indices were archived.
_h = open(os.path.join(HERE, "history.py")).read()
_fb = _h.split("def sync_futures_candles")[1]
_fb = _fb[:_fb.index("\ndef ")] if "\ndef " in _fb else _fb
check("toDate is the NEXT day, not the same day",
      "_td2(days=1)" in _fb,
      "(day, day) returns bars for an INDEX future and NOTHING for a "
      "stock one; (day, day+1) returns 385 for both")
check("instrument type is chosen, not hardcoded FUTIDX",
      'FUTSTK' in _fb and 'UNDERLYINGS' in _fb,
      "index futures are FUTIDX, stock futures FUTSTK")
check("the index path does not depend on the scrip master for this",
      "_ba2.UNDERLYINGS" in _fb,
      "same split as broker_adapter._scrip_and_seg — a CSV outage must "
      "not break the four indices")
check("a symbol the old resolver cannot map falls back to the registry",
      "instrument_registry" in _fb and "_ireg.futures" in _fb,
      "get_current_futures_for_symbols answers 'no exchange mapping for "
      "symbol ADANIENSOL'")

print("\n6c) the registry's futures resolver agrees with the proven one")
import instrument_registry as _ir2
# Local stub rows — STUB in test_instrument_registry.py is a different
# file's fixture and was not in scope here.
def _r(inst, sid, name, exp):
    return {"EXCH_ID": "NSE", "INSTRUMENT": inst, "SECURITY_ID": sid,
            "UNDERLYING_SYMBOL": "ADANIENSOL", "SYMBOL_NAME": name,
            "LOT_SIZE": "675.0", "SM_EXPIRY_DATE": exp}
_ir2._CACHE["rows"] = [
    _r("FUTSTK", "68416", "ADANIENSOL-Sep2026-FUT", "2036-09-29"),
    _r("FUTSTK", "58087", "ADANIENSOL-Aug2026-FUT", "2036-08-25"),
    _r("FUTSTK", "11111", "ADANIENSOL-EXPIRED-FUT", "2020-01-01"),
    _r("OPTSTK", "900", "ADANIENSOL-OPT", "2036-08-25"),
]
_f = _ir2.futures("ADANIENSOL")
check("it returns contracts", len(_f) == 2, str(len(_f)))
check("EXPIRED contracts are dropped",
      all(x["security_id"] != "11111" for x in _f),
      "the CSV keeps them; archiving one writes empty days")
check("sorted by expiry", [x["expiry"] for x in _f] == sorted(
      [x["expiry"] for x in _f]))
check("shape matches the original resolver",
      all({"security_id", "symbol_name", "expiry"} <= set(x) for x in _f),
      "callers must not branch on which resolver answered")

print("\n7) a failing lookup degrades quietly, it does not raise")
try:
    import dhan_scrip_master as _sm2
    _keep = _sm2.get_current_futures_for_symbols
    _sm2.get_current_futures_for_symbols = lambda *a, **k: {}
    got = history.sync_futures_candles(StubDhan(), "NIFTY", DAY,
                                       log=lambda m: None)
    check("no contract resolved -> 0, not an exception", got == 0, str(got))
finally:
    _sm2.get_current_futures_for_symbols = _keep

if _real_sm:
    import dhan_scrip_master as _sm3
    _sm3.get_current_futures_for_symbols = _real_sm

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all futures-candle-archive checks passed")
