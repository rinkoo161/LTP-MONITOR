"""v58.9 (part 5, item 8) — tests for a real gap found from a live
report: "Candles older than the current day are not visible. it should
have all the candles in DB."

Root cause: the Live Chart's own history query hard-cut at
`ts >= today_start`, discarding any older rows even though they
genuinely exist — MarketDataAgent's per-tick candle builder
(_build_candle) runs continuously server-side, independent of any
browser connection, and persists every completed minute to the same
security_id ("{symbol}_SPOT_{interval}m"). Nothing ever prunes it —
confirmed by searching for any DELETE against the candles table and
finding none. The database genuinely has multi-day history sitting
there; the chart simply never asked for it.

Widened to a configurable, interval-scaled lookback instead of "today
only." This reopens a DIFFERENT, pre-existing documented risk — the
candles table still contains flat weekend/evening keepalive bars
persisted before _build_candle was gated on market_open() (2026-07-26)
— so the existing _in_market_session() read-side filter (already used
for the indicator path) is applied here too now that this query can
reach back far enough to encounter that older contaminated data.

Run:  python3 test_chart_multiday_history.py
"""
import datetime as _dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
import agents
import app
import config
import history

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


SYM = "CHARTHISTTEST2"
SEC = f"{SYM}_SPOT_1m"


def trading_day_before(base, n_trading_days):
    """n-th prior trading day (Mon-Fri) before `base`, explicit and
    deterministic — no while-loop collisions between distinct calls."""
    d = base
    remaining = n_trading_days
    while remaining > 0:
        d -= _dt.timedelta(days=1)
        if d.weekday() < 5:
            remaining -= 1
    return d


print("1) config keys registered, and the window is bounded")
# v59.95 — these asserted the exact literals 5 / 20 / 60, encoding the
# original "interval-scaled for a comparable total candle count" rule
# (5x375 ~= 20x75 ~= 60x25 ~= 1,500-1,900 candles each).
#
# 1m was deliberately raised to 30 on an operator request, after a live
# report that the chart showed one session and would not scroll back.
# That KNOWINGLY breaks the comparable-payload rule — 30x375 is ~11,000
# candles, several times the other two — because scrollback was worth
# more than payload symmetry here. Measured cost 0.73 MB, and
# lightweight-charts v5 conflates points when zoomed out.
#
# So the literals are gone and what remains is the property that
# actually matters: every key is REGISTERED (config.save() silently
# drops anything not in DEFAULTS, so an unregistered key vanishes on
# first save) and every window is positive and bounded. A test that
# pins a value the operator is expected to tune reports the tuning as a
# failure.
for _iv, _cap in (("1", 60), ("5", 90), ("15", 120)):
    _k = f"chart_history_days_{_iv}m"
    _v = config.DEFAULTS.get(_k)
    check(f"{_k} registered", _k in config.DEFAULTS)
    check(f"{_k} is a sane bounded window", isinstance(_v, int) and 0 < _v <= _cap,
          f"{_v} (must be 1..{_cap} days)")
check("1m does not exceed the 15m window",
      config.DEFAULTS["chart_history_days_1m"]
      <= config.DEFAULTS["chart_history_days_15m"],
      "1m is ~15x the candles per day; it should never span the longest "
      "window as well")
app_src = open("app.py").read()
check("all three declared on SettingsIn",
      all(f"chart_history_days_{i}m: int" in app_src for i in ("1", "5", "15")))

print("\n2) real multi-day data (3 distinct trading days, no day "
     "collisions) is correctly retrieved within the lookback window")
conn = history._conn()
conn.execute("DELETE FROM candles WHERE security_id=?", (SEC,))
conn.commit()
now = agents.now_ist()
rows = []
for n in (1, 2, 3):
    day = trading_day_before(now, n).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(20):
        ts = int((day + _dt.timedelta(minutes=i)).timestamp())
        rows.append((SEC, ts, 100 + i, 101 + i, 99 + i, 100.5 + i, None, None))
conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", rows)
conn.commit()

today = now.strftime("%Y-%m-%d")
today_start = int(agents.datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=agents.IST).timestamp())
cfg = config.load()
lookback_days = cfg.get("chart_history_days_1m", 5)
history_cutoff = today_start - lookback_days * 86400
fetched = conn.execute(
    "SELECT ts,o,h,l,c FROM candles WHERE security_id=? AND ts>=? ORDER BY ts",
    (SEC, history_cutoff)).fetchall()
check("all 3 distinct trading days' worth of candles (60 total) are "
      "retrieved, not just today's",
      len(fetched) == 60, str(len(fetched)))

print("\n3) the pre-existing out-of-session keepalive contamination risk "
     "is guarded against now that the query reaches back further")
# 2026-08-01 — was "the most recent weekend day", starting at `now`. Two
# problems, both only visible on a weekend:
#
#   1. starting at `now` meant that run ON a Saturday the loop never
#      executed and the "weekend" row was TODAY, so section 4 (which
#      asserts the old today-only query sees nothing) failed;
#   2. starting a day earlier does not fix it either — on a Saturday the
#      5-day lookback window spans Mon-Fri, so there is NO weekend day
#      inside it and the row fell outside the query entirely.
#
# The property under test is that an OUT-OF-SESSION bar is filtered out.
# A weekend day is one instance; an after-hours weekday bar is another,
# always exists inside the window, and is always before today. Testing
# the general property is both more robust and closer to what the filter
# actually promises.
weekend = trading_day_before(now, 1).replace(hour=22, minute=0,
                                             second=0, microsecond=0)
conn.execute("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)",
            (SEC, int(weekend.timestamp()), 999, 999, 999, 999, None, None))
conn.commit()
fetched2 = conn.execute(
    "SELECT ts,o,h,l,c FROM candles WHERE security_id=? AND ts>=? ORDER BY ts",
    (SEC, history_cutoff)).fetchall()
check("the raw query now includes the contaminated out-of-session row (61, "
      "confirming it WOULD have leaked through without the filter)",
      len(fetched2) == 61, str(len(fetched2)))
filtered = [r for r in fetched2 if app._in_market_session(r[0])]
check("_in_market_session() correctly filters it back out, leaving "
      "exactly the 60 real in-session candles",
      len(filtered) == 60, str(len(filtered)))

print("\n4) a query with the OLD 'today only' cutoff would have missed "
     "all 3 seeded days entirely (proves this is a genuine fix, not a "
     "no-op) — confirms the reported symptom was real")
old_style = conn.execute(
    "SELECT ts,o,h,l,c FROM candles WHERE security_id=? AND ts>=? ORDER BY ts",
    (SEC, today_start)).fetchall()
check("the OLD query style returns 0 rows for this fixture (all "
      "seeded data is from PRIOR days, none from today)",
      len(old_style) == 0, str(len(old_style)))

conn.execute("DELETE FROM candles WHERE security_id=?", (SEC,))
conn.commit()

print("\n5) the actual shipped source contains the fix, not just a "
     "parallel reimplementation in this test")
check("app.py computes history_cutoff using the new per-interval config",
      "history_cutoff = today_start - lookback_days * 86400" in app_src)
check("the query uses history_cutoff, not the old today_start cutoff",
      "(security_id, history_cutoff)).fetchall()" in app_src)
check("_in_market_session is applied to this specific query's results",
      "if _in_market_session(r[0])" in app_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
