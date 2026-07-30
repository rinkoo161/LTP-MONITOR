"""v58.71 — out-of-session candles must not be STORED, not merely
filtered on the way out.

Reported as "straight candles on the NIFTY chart". The store held
13,577 out-of-session rows:

    NIFTY_SPOT_15m  236/365 (64%)   NIFTY_FUT_1m 1953/3407 (57%)
    NIFTY_SPOT_5m   209/515 (40%)   NIFTY_SPOT_1m    0/775  (0%)

The 1m series being clean is the tell: its PRODUCER was gated by the
2026-07-26 keepalive fix, and the others never were. Gating producers
one at a time is what produced that split, so the invariant belongs at
the single write boundary they all share — the read filters and the
prune endpoint stay as defence in depth for data already written.

Runs entirely against a temp DB. A test that writes candles into
~/.ltp-monitor/history.db to prove candles are written correctly is the
same mistake this session already made with the shadow journal.
"""
import os, sys, tempfile, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import agents
import history

history.DB = os.path.join(tempfile.mkdtemp(), "gate.db")
history._SCHEMA_READY = False

IST = agents.IST


def ts_at(y, m, d, hh, mm):
    return int(dt.datetime(y, m, d, hh, mm, tzinfo=IST).timestamp())


def bar(ts, price=24000.0):
    return {"ts": ts, "o": price, "h": price, "l": price, "c": price,
            "v": 0, "oi": None}


def stored(sec):
    c = history._conn()
    n = c.execute("SELECT COUNT(*) FROM candles WHERE security_id=?", (sec,)).fetchone()[0]
    c.close()
    return n


# 2026-07-30 was a Thursday; 2026-08-01 a Saturday.
IN_SESSION = [ts_at(2026, 7, 30, 9, 20), ts_at(2026, 7, 30, 12, 0),
              ts_at(2026, 7, 30, 15, 29)]
OUT_SESSION = [ts_at(2026, 7, 30, 15, 45),   # after the close
               ts_at(2026, 7, 30, 19, 36),   # the evening keepalive seen live
               ts_at(2026, 7, 30, 3, 10),    # small hours
               ts_at(2026, 8, 1, 11, 0)]     # a Saturday

print("1) the reported symptom cannot be stored any more")
n = history.upsert_candles("ZZGATE", [bar(t) for t in OUT_SESSION])
check("writing ONLY out-of-session bars persists nothing", n == 0 and stored("ZZGATE") == 0,
      f"returned {n}, stored {stored('ZZGATE')}")

print("\n2) in-session data is untouched")
n = history.upsert_candles("ZZGATE2", [bar(t) for t in IN_SESSION])
check("all in-session bars persist", n == 3 and stored("ZZGATE2") == 3,
      f"returned {n}, stored {stored('ZZGATE2')}")

print("\n3) a mixed batch keeps the good half")
n = history.upsert_candles("ZZGATE3", [bar(t) for t in IN_SESSION + OUT_SESSION])
check("only the in-session rows are written", n == 3 and stored("ZZGATE3") == 3,
      f"returned {n}, stored {stored('ZZGATE3')}")
check("the return value is rows WRITTEN, not rows offered", n == 3, str(n))
c = history._conn()
kept = [r[0] for r in c.execute("SELECT ts FROM candles WHERE security_id='ZZGATE3'").fetchall()]
c.close()
check("every stored row passes in_market_session",
      all(agents.in_market_session(t) for t in kept))
check("no evening keepalive survived", ts_at(2026, 7, 30, 19, 36) not in kept)

print("\n4) dropping is observable, not silent")
check("the drop count is recorded per security_id",
      history.DROPPED_OUT_OF_SESSION.get("ZZGATE3") == 4,
      str(history.DROPPED_OUT_OF_SESSION.get("ZZGATE3")))
check("and totals across the process", history.DROPPED_OUT_OF_SESSION.get("ZZGATE") == 4,
      str(history.DROPPED_OUT_OF_SESSION.get("ZZGATE")))

print("\n5) the escape hatch is explicit, never the default")
n = history.upsert_candles("ZZGATE4", [bar(t) for t in OUT_SESSION],
                           session_only=False)
check("session_only=False stores everything", n == 4 and stored("ZZGATE4") == 4,
      f"returned {n}, stored {stored('ZZGATE4')}")
import inspect
sig = inspect.signature(history.upsert_candles)
check("session_only defaults to True",
      sig.parameters["session_only"].default is True)

print("\n6) real callers still work (they all write minute bars)")
# sync_index_history fetches interval "1"; upsert_index_candles builds
# {symbol}_SPOT_{tf}m. Both go through the same boundary.
n = history.upsert_candles("13", [bar(t) for t in IN_SESSION])
check("numeric-security-id backfill path unaffected", n == 3)
check("empty input is still a no-op", history.upsert_candles("ZZGATE5", []) == 0)

print("\n7) the gate is at the write boundary, not bolted onto a caller")
src = open("history.py").read()
_code = [ln.split("#", 1)[0] for ln in src.splitlines()]
i = next(n for n, ln in enumerate(_code) if "def upsert_candles" in ln)
body = "\n".join(_code[i:i + 45])
check("upsert_candles itself consults in_market_session",
      "agents.in_market_session" in body)
check("and it is the only INSERT INTO candles for OHLC",
      sum("INSERT OR REPLACE INTO candles" in ln for ln in _code) == 1)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
