"""v55 — tests for the backtest trade-record enrichment that unblocks
the Backtest-page chart overlay: entry_ts/exit_ts/entry_spot/exit_spot
added to trades across all three replay loops (spreads, momentum,
price-action), plus a compact trades_detail field on metrics().

Run:  python3 test_backtest_chart_data.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("deletes rows")
import backtester
import history

results = []
SYM = "BTCHARTTEST"


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


print("1) metrics() trades_detail — unit-level shape")
fake_trades = [
    {"day": "2026-07-01", "strategy": "bull_put_spread", "pnl": 500,
     "risk": 2000, "reason": "profit target",
     "entry_ts": 1751350800, "exit_ts": 1751370000,
     "entry_spot": 23800, "exit_spot": 23850},
    {"day": "2026-07-02", "strategy": "bull_put_spread", "pnl": -300,
     "risk": 2000, "reason": "loss limit",
     "entry_ts": 1751437200, "exit_ts": 1751456400,
     "entry_spot": 23700, "exit_spot": 23600},
]
m = backtester.metrics(fake_trades)
check("trades_detail present with 2 entries", len(m.get("trades_detail", [])) == 2)
td = m["trades_detail"][0]
check("trades_detail entry has entry_ts/exit_ts/entry_spot/exit_spot/pnl/reason/day",
      all(k in td for k in ("entry_ts", "exit_ts", "entry_spot", "exit_spot",
                            "pnl", "reason", "day")), str(td))
check("trades_detail is compact — no 'risk' field leaked through",
      "risk" not in td)

m_partial = backtester.metrics(fake_trades + [
    {"day": "2026-07-03", "strategy": "x", "pnl": 10, "risk": 1, "reason": "EOD"}])
check("a trade missing entry_ts/exit_ts is excluded from trades_detail "
      "(defensive filter, not a crash)",
      len(m_partial["trades_detail"]) == 2, str(len(m_partial["trades_detail"])))

print("\n2) end-to-end: replay_pa populates real entry_ts/exit_ts/spot values")
conn = history._conn()
conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
            (f"{SYM}_IDX", SYM, "idx"))
conn.commit()

# A day with a clean 5/13 EMA cross the ema_mtf strategy should catch,
# same shape as the fixture used in test_strategy7.py.
day = "2026-01-05"   # a Monday, arbitrary past date
base_ts = int(time.mktime(time.strptime(day, "%Y-%m-%d"))) + 9 * 3600 + 15 * 60
closes = []
for i in range(90):
    closes.append(23800 - i * 2 + math.sin(i / 5) * 90)
for i in range(30):
    closes.append(closes[-1] + 14)
rows = [{"ts": base_ts + i * 60, "o": c, "h": c + 1.5, "l": c - 1.5, "c": c,
         "v": None, "oi": None} for i, c in enumerate(closes)]
history.upsert_candles(f"{SYM}_IDX", rows)

trades = backtester.replay_pa(SYM, "ema_mtf", params={
    "fast": 5, "slow": 13, "mtf_confirm": 0, "max_trades_per_day": 5},
    days=[day])
check("replay_pa produced at least one trade on the synthetic fixture",
      len(trades) >= 1, f"{len(trades)} trades")
if trades:
    t = trades[0]
    check("entry_ts is a real timestamp within the synthetic day's range",
          t.get("entry_ts") and base_ts <= t["entry_ts"] <= base_ts + 90 * 60,
          str(t.get("entry_ts")))
    check("exit_ts is at or after entry_ts", t.get("exit_ts", 0) >= t.get("entry_ts", 1),
          f"entry={t.get('entry_ts')} exit={t.get('exit_ts')}")
    check("entry_spot/exit_spot are real index price levels, not None/0",
          t.get("entry_spot") and t.get("exit_spot") and
          20000 < t["entry_spot"] < 30000 and 20000 < t["exit_spot"] < 30000,
          f"entry_spot={t.get('entry_spot')} exit_spot={t.get('exit_spot')}")
    m2 = backtester.metrics(trades)
    check("metrics() on real replay_pa output includes a populated trades_detail",
          len(m2["trades_detail"]) == len(trades), str(len(m2["trades_detail"])))

conn = history._conn()
conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
conn.commit()

print("\n3) source-level guard: replay_spreads and replay_momentum both "
     "capture + propagate the same 4 fields (regression guard against a "
     "future refactor silently dropping them — these two are chain-\n"
     "reconstruction based and expensive to fixture end-to-end, so this "
     "checks the actual source rather than skipping verification)")
src = open("backtester.py").read()

def between(a, b):
    i = src.index(a)
    j = src.index(b, i)
    return src[i:j]

spreads_fn = between("def replay_spreads(", "def _eval_with_params(")
check("replay_spreads captures entry_ts/entry_spot at position open",
      '"entry_ts": ts, "entry_spot": chain["spot"]' in spreads_fn)
check("replay_spreads' closed-trade record includes all 4 fields",
      '"entry_ts": open_sp["entry_ts"]' in spreads_fn and
      '"exit_ts": ts' in spreads_fn and
      '"entry_spot": open_sp["entry_spot"]' in spreads_fn and
      '"exit_spot": chain["spot"]' in spreads_fn)

momentum_fn = between("def replay_momentum(", "def _resample(")
check("replay_momentum captures entry_ts/entry_spot at position open",
      '"entry_ts": ts, "entry_spot": chain["spot"]' in momentum_fn)
check("replay_momentum's closed-trade record includes all 4 fields",
      '"entry_ts": pos["entry_ts"], "exit_ts": ts' in momentum_fn and
      '"entry_spot": pos["entry_spot"], "exit_spot": chain["spot"]' in momentum_fn)

print("\n4) /api/backtest/day-candles endpoint returns real seeded candles")
from fastapi.testclient import TestClient
import app
client = TestClient(app.app)
conn = history._conn()
conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
            (f"{SYM}_IDX", SYM, "idx"))
seed_rows = [(f"{SYM}_IDX", 1767606000 + i * 60, 100, 101, 99, 100.5, None, None)
            for i in range(10)]
conn.executemany("INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?)", seed_rows)
conn.commit()
r = client.get(f"/api/backtest/day-candles?symbol={SYM}&day=2026-01-05")
check("endpoint returns 200", r.status_code == 200, str(r.status_code))
check("endpoint returns all 10 seeded candles with time/OHLC keys",
      len(r.json().get("candles", [])) == 10 and
      all(k in r.json()["candles"][0] for k in ("time", "open", "high", "low", "close")),
      str(r.json().get("candles", [None])[0]))
conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{SYM}%",))
conn.execute("DELETE FROM instruments WHERE symbol=?", (SYM,))
conn.commit()

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
