"""v58.2 — tests for two bugs found from a live screenshot report:
1. SENSEX's shown change was off by ~48 points from real broker apps.
   Root cause: prev_close_for() always used whichever broker is
   ACTIVE in Settings, not necessarily Dhan, and never used the
   dedicated SENSEX fallback client get_chain() already has.
2. A "False Breakout" marker stayed visibly anchored to yesterday's
   session even after today's market open. Root cause: the marker
   anchor timestamp fell back to the last HISTORICAL bar when no live
   tick had arrived yet this connection — which, right after open, is
   still yesterday's final candle.

Run:  python3 test_prevclose_and_marker_anchor.py
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


class FakeClient:
    def __init__(self, candles, raise_err=None):
        self._candles = candles
        self._raise = raise_err

    def intraday(self, sym, interval):
        if self._raise:
            raise self._raise
        return {"candles": self._candles}


def last_friday_from(now):
    """The most recent Friday STRICTLY BEFORE today.

    2026-07-31 — this walked back from TODAY, so run on a Friday it
    returned today. The fixture then puts the "previous session" on the
    current date: prev_close_for finds no prior-day candle and correctly
    returns None, and resolve_anchor is handed a today-timestamp it is
    right to keep. Four checks failed for reasons that had nothing to do
    with the code under test.

    It passed at 23:5x on Thursday and failed at 02:00 the same night,
    which is the tell — a test that only works Mon-Thu reports the
    calendar, not the code. Starting the walk a day back makes the
    fixture mean what it says on every weekday.
    """
    d = (now.replace(hour=15, minute=15, second=0, microsecond=0)
         - _dt.timedelta(days=1))
    while d.weekday() != 4:
        d -= _dt.timedelta(days=1)
    return d


def make_candles(day, closes, hour=9, minute=15, step_min=15):
    return [{"time": int((day.replace(hour=hour, minute=minute) +
                         _dt.timedelta(minutes=step_min * i)).timestamp()),
            "open": c, "high": c + 1, "low": c - 1, "close": c}
           for i, c in enumerate(closes)]


now = agents.now_ist()
friday = last_friday_from(now)

print("1) prev_close_for('SENSEX') uses the dedicated Dhan fallback, "
     "not whichever broker is active")
friday_candles = make_candles(friday, [100 + i for i in range(25)])
today_candles = make_candles(now, [200 + i for i in range(3)])
real_fallback_fn = app._dhan_fallback_client
real_dhan_client = app.dhan_client
app._dhan_fallback_client = lambda: FakeClient(friday_candles + today_candles)
app._prev_close.pop("SENSEX", None)
try:
    pc = app.prev_close_for("SENSEX")
    check("returns Friday's actual last close (124), not None or "
          "today's data leaking in", pc == 124, str(pc))
finally:
    app._dhan_fallback_client = real_fallback_fn
    app._prev_close.pop("SENSEX", None)

print("\n2) SENSEX prefers the Dhan fallback even when the ACTIVE broker "
     "would otherwise be used and can't serve it")
app.dhan_client = lambda: FakeClient([], raise_err=RuntimeError("wrong broker"))
app._dhan_fallback_client = lambda: FakeClient(friday_candles)
app._prev_close.pop("SENSEX", None)
try:
    pc = app.prev_close_for("SENSEX")
    check("uses the fallback's data (124) instead of failing via the "
          "wrong active broker", pc == 124, str(pc))
finally:
    app.dhan_client = real_dhan_client
    app._dhan_fallback_client = real_fallback_fn
    app._prev_close.pop("SENSEX", None)

print("\n3) non-SENSEX symbols still use the normal active-broker path "
     "(the fallback preference is SENSEX-specific, not universal)")
nifty_candles = make_candles(friday, [50 + i for i in range(25)])
app.dhan_client = lambda: FakeClient(nifty_candles)
app._prev_close.pop("NIFTY", None)
try:
    pc = app.prev_close_for("NIFTY")
    check("NIFTY still resolves via the active broker normally",
          pc == 74, str(pc))
finally:
    app.dhan_client = real_dhan_client
    app._prev_close.pop("NIFTY", None)

print("\n4) day-boundary comparison is explicitly IST-aware, not "
     "dependent on the server's ambient system timezone")
check("agents.IST is used for the fromtimestamp conversion (source check)",
      "tz=agents.IST" in open("app.py").read())
check("today comparison uses agents.now_ist(), not date.today()",
      "today = agents.now_ist().date().isoformat()" in open("app.py").read())

print("\n5) marker anchor timestamp: a stale PRIOR-DAY historical bar "
     "must not be used to anchor a CURRENT institutional/smart-money "
     "marker")


def resolve_anchor(anchor_ts):
    if anchor_ts is not None:
        anchor_day = _dt.datetime.fromtimestamp(anchor_ts, tz=agents.IST).date()
        if anchor_day != agents.now_ist().date():
            return None
    return anchor_ts


yesterday_bar_ts = int(friday.replace(hour=15, minute=29).timestamp())
today_bar_ts = int(now.timestamp())
check("a bar from a prior day resets to None (falls through to wall-clock now)",
      resolve_anchor(yesterday_bar_ts) is None)
check("a bar from today is kept unchanged", resolve_anchor(today_bar_ts) == today_bar_ts)
check("None stays None (no candle data at all — unrelated case, unaffected)",
      resolve_anchor(None) is None)

print("\n6) the actual shipped source contains the fix, not just my "
     "standalone reimplementation of the logic above")
src = open("app.py").read()
check("anchor_ts is reset when it falls on a day other than today",
      "anchor_day != agents.now_ist().date()" in src and
      "anchor_ts = None" in src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
