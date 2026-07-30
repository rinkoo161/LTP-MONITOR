"""v58.9 — tests for a real bug found from a live report: ALL FOUR
indices' displayed % change was off from TradingView by a small,
consistent amount, even with broker="dhan" already active (ruling out
the earlier SENSEX-specific broker-mismatch fix as the cause here).

Root cause: prev_close_for() derived the previous close by
reconstructing it from 15-minute candles, one step removed from the
exchange's own official reference print. Dhan's quote API already
returns an authoritative `previous_close_price` field directly
(already used this way for OPTION LEGS in broker_adapter.py's
_leg() — only this index-level path took the less direct route).

Fix tries the authoritative quote_batch() field FIRST and falls back
to the existing candle-reconstruction only if that's unavailable or
errors — kept as a genuine safety net since the exact schema Dhan
returns for an INDEX quote (vs the already-confirmed option-leg one)
hasn't been verified against a live server from this environment.

Run:  python3 test_authoritative_prev_close.py
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
import app

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def last_friday_candles(base_val=100):
    now = agents.now_ist()
    d = now.replace(hour=15, minute=15, second=0, microsecond=0)
    while d.weekday() != 4:
        d -= _dt.timedelta(days=1)
    return [{"time": int((d.replace(hour=9, minute=15) +
                        _dt.timedelta(minutes=15 * i)).timestamp()),
            "open": base_val, "high": base_val + 1, "low": base_val - 1,
            "close": base_val + i} for i in range(25)]


real_dhan_client = app.dhan_client

print("1) authoritative quote field used when available — matches "
     "TradingView's actual reference in the reported case")


class QuoteWithField:
    def quote_batch(self, seg_map):
        return {"IDX_I": {"13": {"previous_close_price": 23767.45,
                                 "last_price": 23995.95}}}


app.dhan_client = lambda: QuoteWithField()
app._prev_close.pop("NIFTY", None)
try:
    pc = app.prev_close_for("NIFTY")
    check("returns the authoritative value (23767.45), not a "
          "candle-reconstructed approximation",
          pc == 23767.45, str(pc))
finally:
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)

print("\n2) falls back to candle reconstruction when the quote lacks "
     "the field entirely — not a crash, not a None result")


class QuoteNoField:
    def quote_batch(self, seg_map):
        return {"IDX_I": {"13": {"last_price": 23995.95}}}

    def intraday(self, sym, interval):
        return {"candles": last_friday_candles()}


app.dhan_client = lambda: QuoteNoField()
app._prev_close.pop("NIFTY", None)
try:
    pc2 = app.prev_close_for("NIFTY")
    check("falls back correctly to the candle-derived value (124)",
          pc2 == 124, str(pc2))
finally:
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)

print("\n3) falls back to candle reconstruction when quote_batch itself "
     "raises — the safety net actually catches a real exception")


class QuoteRaises:
    def quote_batch(self, seg_map):
        raise RuntimeError("network error")

    def intraday(self, sym, interval):
        return {"candles": last_friday_candles()}


app.dhan_client = lambda: QuoteRaises()
app._prev_close.pop("NIFTY", None)
try:
    pc3 = app.prev_close_for("NIFTY")
    check("still returns the candle-derived value despite the exception",
          pc3 == 124, str(pc3))
finally:
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)

print("\n4) a broker client without quote_batch at all (e.g. Kotak/"
     "Zerodha) is handled gracefully, not an AttributeError")


class NoQuoteBatchClient:
    def intraday(self, sym, interval):
        return {"candles": last_friday_candles()}


app.dhan_client = lambda: NoQuoteBatchClient()
app._prev_close.pop("NIFTY", None)
try:
    pc4 = app.prev_close_for("NIFTY")
    check("falls straight through to candle reconstruction, no crash",
          pc4 == 124, str(pc4))
finally:
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)

print("\n5) SENSEX still prefers the dedicated Dhan fallback client "
     "(the earlier v58.2 fix), and that fallback ALSO gets the new "
     "authoritative-quote-first treatment")


class SensexQuoteWithField:
    def quote_batch(self, seg_map):
        return {"IDX_I": {"51": {"previous_close_price": 76059.77,
                                 "last_price": 76835.78}}}


real_fallback = app._dhan_fallback_client
app.dhan_client = lambda: NoQuoteBatchClient()   # active broker can't serve SENSEX
app._dhan_fallback_client = lambda: SensexQuoteWithField()
app._prev_close.pop("SENSEX", None)
try:
    pc5 = app.prev_close_for("SENSEX")
    check("SENSEX uses the fallback client's authoritative quote value",
          pc5 == 76059.77, str(pc5))
finally:
    app.dhan_client = real_dhan_client
    app._dhan_fallback_client = real_fallback
    app._prev_close.pop("SENSEX", None)

print("\n6) THE REAL ROOT CAUSE CONFIRMED FROM A LIVE LOG: NIFTY/FINNIFTY "
     "succeeded individually (proving the field name guess was "
     "correct all along), but BANKNIFTY/SENSEX hit 429 Too Many "
     "Requests because all 4 quote_batch() calls fired in quick "
     "succession against an endpoint already under contention from "
     "futures polling. Fixed by batching all 4 symbols into ONE call.")


class BatchFakeFallback:
    def __init__(self):
        self.calls = 0

    def quote_batch(self, req):
        self.calls += 1
        return {"IDX_I": {str(sid): {"previous_close_price": 10000 + sid}
                          for sid in req["IDX_I"]}}


app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
fake = BatchFakeFallback()
app._dhan_fallback_client = lambda: fake
try:
    r1 = app.prev_close_for("NIFTY")
    r2 = app.prev_close_for("BANKNIFTY")
    r3 = app.prev_close_for("FINNIFTY")
    r4 = app.prev_close_for("SENSEX")
    check("all 4 symbols correctly resolved a real previous_close value",
          all(v is not None for v in (r1, r2, r3, r4)), str((r1, r2, r3, r4)))
    check("exactly ONE quote_batch() call was made for all 4 symbols "
          "combined — not 4 separate ones (this is the actual fix: "
          "removing the self-inflicted rate-limit pressure, not "
          "retrying around it)",
          fake.calls == 1, str(fake.calls))
finally:
    app._dhan_fallback_client = real_fallback
    for s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        app._prev_close.pop(s, None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n7) the batch fetch runs AT MOST ONCE PER DAY, even across many "
     "repeated prev_close_for() calls for the same or different "
     "symbols (matching how prev_close itself is only needed once "
     "per symbol per day)")
app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
fake2 = BatchFakeFallback()
app._dhan_fallback_client = lambda: fake2
try:
    app.prev_close_for("NIFTY")
    app.prev_close_for("NIFTY")
    app.prev_close_for("BANKNIFTY")
    app.prev_close_for("NIFTY")
    check("still exactly 1 batch call after 4 total prev_close_for() "
         "invocations",
          fake2.calls == 1, str(fake2.calls))
finally:
    app._dhan_fallback_client = real_fallback
    for s in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        app._prev_close.pop(s, None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n8) when the batch call itself fails (e.g. still rate-limited "
     "even as one call), the function gracefully falls through to "
     "the per-symbol safety net rather than crashing or returning "
     "a stale/wrong value silently")


class FailingBatchFallback:
    def quote_batch(self, req):
        raise RuntimeError("429 Too Many Requests")


app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
app._dhan_fallback_client = lambda: FailingBatchFallback()
app.dhan_client = lambda: None
try:
    r = app.prev_close_for("NIFTY")
    check("returns None gracefully (no other client available) rather "
         "than raising an exception up to the caller",
          r is None)
finally:
    app.dhan_client = real_dhan_client
    app._dhan_fallback_client = real_fallback
    app._prev_close.pop("NIFTY", None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n9) THE SECOND REAL REGRESSION FOUND FROM LIVE TESTING: NIFTY and "
     "FINNIFTY both started showing 0 (0%) change after part 4 "
     "shipped. Root cause: tested AFTER market close, and Dhan's "
     "previous_close_price appears to reflect the JUST-CONCLUDED "
     "session's own close once the market has shut, not genuinely "
     "yesterday's close. A previous_close that EXACTLY matches "
     "last_price (same response, no extra call) is the tell.")


class SuspiciousBatchFallback:
    def quote_batch(self, req):
        return {"IDX_I": {str(sid): {"previous_close_price": 23995.95,
                                     "last_price": 23995.95}
                          for sid in req["IDX_I"]}}


app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
app._dhan_fallback_client = lambda: SuspiciousBatchFallback()
try:
    r_suspicious = app.prev_close_for("NIFTY")
    check("a previous_close that exactly matches last_price is NOT "
          "trusted/cached (falls through instead of returning the "
          "suspicious value)",
          "NIFTY" not in app._prev_close or app._prev_close["NIFTY"][1] != 23995.95,
          str(app._prev_close.get("NIFTY")))
finally:
    app._dhan_fallback_client = real_fallback
    app._prev_close.pop("NIFTY", None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n10) a GENUINE previous_close (genuinely different from "
     "last_price) is still trusted and cached correctly — the fix "
     "doesn't over-correct and reject everything")


class GenuineBatchFallback:
    def quote_batch(self, req):
        return {"IDX_I": {str(sid): {"previous_close_price": 23800.0,
                                     "last_price": 23995.95}
                          for sid in req["IDX_I"]}}


app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
app._dhan_fallback_client = lambda: GenuineBatchFallback()
try:
    r_genuine = app.prev_close_for("NIFTY")
    check("a genuinely different previous_close IS trusted and returned",
          r_genuine == 23800.0, str(r_genuine))
finally:
    app._dhan_fallback_client = real_fallback
    app._prev_close.pop("NIFTY", None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n11) the SAME sanity check applies to the per-symbol fallback "
     "path too, not just the batch path")


class NoQuoteBatchFallback:
    pass


class SuspiciousPerSymbolClient:
    def quote_batch(self, req):
        return {"IDX_I": {"13": {"previous_close_price": 23995.95,
                                 "last_price": 23995.95}}}


app._prev_close.clear()
app._prev_close_batch_tried = None
app._reset_quote_rate_limit()
app._dhan_fallback_client = lambda: NoQuoteBatchFallback()
app.dhan_client = lambda: SuspiciousPerSymbolClient()
try:
    r_per_symbol = app.prev_close_for("NIFTY")
    check("per-symbol path also distrusts a suspicious exact match",
          "NIFTY" not in app._prev_close or app._prev_close["NIFTY"][1] != 23995.95,
          str(app._prev_close.get("NIFTY")))
finally:
    app._dhan_fallback_client = real_fallback
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
