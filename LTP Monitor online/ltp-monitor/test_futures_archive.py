"""v59.0 item 4 — archive real futures OHLCV+OI, starting now.

Phase A could not run a single strategy on the instrument: futures prices
were never archived, only volume, and only for 5 sessions. Every Phase A
number therefore came from INDEX candles with futures costs applied — a
defensible intraday proxy, not the thing itself. This series is the
gating dependency for any re-test, and it costs nothing to begin today.

The subtle failure this guards against: upsert_volume_history writes a
v-only row for the same security_id using ON CONFLICT DO UPDATE SET v.
Layering an OHLC write on top with a naive REPLACE would null the volume
it had just stored. So the archiver writes o/h/l/c/v/oi in ONE row.
"""
import os, sys, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes candles")
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents, history

print("1) a minute bucket is written with every field")
bus = agents.Bus()
ag = agents.MarketDataAgent(bus, {"get_chain": lambda s: None,
                                  "orders_factory": lambda: None})
# force a bucket inside market hours so the write gate accepts it
base = dt.datetime.now().replace(hour=11, minute=0, second=0, microsecond=0)
while base.weekday() >= 5:
    base -= dt.timedelta(days=1)
t0 = base.timestamp()
real_time = agents.time.time
seq = [t0, t0 + 10, t0 + 20, t0 + 70]        # 3 ticks in bucket A, 1 in B
it = iter(seq)
agents.time.time = lambda: next(it, seq[-1])
try:
    ag._archive_futures_candle("NIFTY", 24000, 1000, {"oi": 500000})
    ag._archive_futures_candle("NIFTY", 24050, 1400, {"oi": 505000})
    ag._archive_futures_candle("NIFTY", 23980, 1800, {"oi": 507000})
    ag._archive_futures_candle("NIFTY", 24010, 2200, {"oi": 509000})   # rollover
finally:
    agents.time.time = real_time

c = history._conn()
rows = c.execute("SELECT ts,o,h,l,c,v,oi FROM candles WHERE security_id='NIFTY_FUT_1m' "
                 "ORDER BY ts").fetchall()
c.close()
check("a completed bucket was persisted", len(rows) >= 1, f"{len(rows)} rows")
if rows:
    ts, o, h, l, cl, v, oi = rows[0]
    print(f"     o {o} h {h} l {l} c {cl} v {v} oi {oi}")
    check("open is the first tick of the bucket", o == 24000, str(o))
    check("high is the max", h == 24050, str(h))
    check("low is the min", l == 23980, str(l))
    check("close is the last tick", cl == 23980, str(cl))
    check("volume is the DELTA within the bucket, not the cumulative total",
          v == 800, f"{v} (1800-1000)")
    check("open interest is captured", oi == 507000, str(oi))
    check("price and volume live in ONE row — no v-only overwrite risk",
          None not in (o, h, l, cl, v))

print("\n2) it announces the start date once, not every bar")
msgs = [m for m in bus.feed if "archive ACTIVE" in m]
check("the start is logged exactly once", len(msgs) == 1, f"{len(msgs)} messages")
check("...and says why it matters", msgs and "re-test" in msgs[0], msgs[0][-48:] if msgs else "")

print("\n3) out-of-hours bars are refused by the write gate")
oh = dt.datetime.now().replace(hour=22, minute=0, second=0, microsecond=0).timestamp()
it2 = iter([oh, oh + 70])
agents.time.time = lambda: next(it2, oh + 70)
try:
    ag._fut_candle_state.pop("BANKNIFTY", None)
    ag._archive_futures_candle("BANKNIFTY", 57000, 100, {"oi": 1})
    ag._archive_futures_candle("BANKNIFTY", 57010, 200, {"oi": 2})
finally:
    agents.time.time = real_time
c = history._conn()
n = c.execute("SELECT COUNT(*) FROM candles WHERE security_id='BANKNIFTY_FUT_1m'").fetchone()[0]
c.close()
check("a 22:00 futures bar is not stored", n == 0,
      f"{n} rows — the v58.71 write gate must refuse keepalive contamination")

print("\n4) it hangs off the existing tick path, not a new poll loop")
src = open("agents.py").read()
check("called from the futures tick handler",
      "_archive_futures_candle(sym, ltp, cum_volume, tick)" in src)
check("no independent thread or timer was added",
      "_fut_archive_thread" not in src and "threading.Timer" not in src)
check("failures are throttled and reported, not swallowed",
      "futures candle archive FAILED" in src and "should_log_throttled" in src)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
