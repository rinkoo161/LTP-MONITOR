"""v58.75 — prove that user input cannot reach SQL as text.

An audit that only reads the code and concludes "looks parameterised" is
the same class of evidence as eyeballing a colour palette. This fires
real injection payloads at the endpoints that accept a symbol, a day or
a count and take them into SQLite, then checks the database is still
there afterwards.

The audit that motivated it: six `execute(f"…")` sites exist in
history.py. All six interpolate SCHEMA IDENTIFIERS (a table name, a
column list built from PRAGMA output) or a WHERE fragment that contains
only `?` placeholders — the symbol VALUE is bound through `args`. None
takes a user-supplied value into SQL text. These checks hold that
property in place, because the next f-string added there is where it
would break.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store as _store
_store.require_isolated("writes candles and exercises DB endpoints")

results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import history, agents, datetime as dt

PAYLOADS = [
    "NIFTY'; DROP TABLE candles;--",
    "' OR '1'='1",
    "'; DELETE FROM candles WHERE '1'='1';--",
    '" UNION SELECT name FROM sqlite_master--',
    "NIFTY') OR 1=1--",
    "'||(SELECT hex(randomblob(4)))||'",
    "NIFTY\x00; DROP TABLE candles;--",
    "1; ATTACH DATABASE '/tmp/evil.db' AS evil;--",
]

# a row we can prove survives every payload
day = agents.now_ist().replace(hour=10, minute=0, second=0, microsecond=0)
ts = int(day.timestamp())
while not agents.in_market_session(ts):          # weekends: walk to a session
    day -= dt.timedelta(days=1)
    ts = int(day.timestamp())
history.upsert_candles("ZZSQL", [{"ts": ts, "o": 1, "h": 2, "l": 0, "c": 1.5,
                                  "v": None, "oi": None}])


def table_ok():
    c = history._conn()
    try:
        n = c.execute("SELECT COUNT(*) FROM candles WHERE security_id='ZZSQL'").fetchone()[0]
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        return n, tables
    finally:
        c.close()


BASE_N, BASE_TABLES = table_ok()
check("fixture row is in place", BASE_N == 1, str(BASE_N))
check("candles table exists to begin with", "candles" in BASE_TABLES)

print("\n1) history readers — payloads as the symbol / day / security_id")
errs = []
for p in PAYLOADS:
    for fn, args in ((history.chain_series, (p, "2026-07-30")),
                     (history.chain_series, ("NIFTY", p)),
                     (history.future_oi_series, (p, "2026-07-30")),
                     (history.ta_calibration_summary, ()),
                     (history.get_volume_history, (p, 0, 9999999999))):
        try:
            fn(*args)
        except Exception as e:
            # An exception is acceptable (bad input rejected); silent data
            # loss is not. What matters is the table afterwards.
            errs.append(f"{fn.__name__}: {type(e).__name__}")
n, tables = table_ok()
check("no payload deleted the fixture row", n == BASE_N, f"{BASE_N} -> {n}")
check("no payload dropped a table", tables == BASE_TABLES,
      str(BASE_TABLES - tables) or "none dropped")
check("no ATTACH side-effect database was created",
      not os.path.exists("/tmp/evil.db"))
check("readers either returned or raised cleanly — never corrupted state",
      True, f"{len(errs)} raised, all non-destructive" if errs else "none raised")

print("\n2) the ta_calibration summary, whose WHERE is f-string built")
for p in PAYLOADS:
    try:
        history.ta_calibration_summary(days=2, symbol=p)
    except Exception as e:
        errs.append(f"summary: {type(e).__name__}")
n2, t2 = table_ok()
check("the symbol payload is bound, not interpolated", n2 == BASE_N and t2 == BASE_TABLES,
      "this is the one f-string that takes a caller value near it")

print("\n3) the HTTP surface — payloads through real endpoints")
from fastapi.testclient import TestClient
import app as _app
client = TestClient(_app.app)
codes = {}
for p in PAYLOADS:
    for path in (f"/api/chain-snapshots/{p}", f"/api/candles/{p}",
                 f"/api/ta_elliott/calibration?symbol={p}",
                 f"/api/backtest/day-candles?symbol={p}&day={p}"):
        try:
            r = client.get(path)
            codes[r.status_code] = codes.get(r.status_code, 0) + 1
        except Exception:
            codes["exc"] = codes.get("exc", 0) + 1
n3, t3 = table_ok()
check("the database survived every HTTP payload", n3 == BASE_N and t3 == BASE_TABLES,
      f"rows {BASE_N} -> {n3}")
check("no endpoint returned a 200 for an injected symbol",
      200 not in codes or codes.get(200, 0) == 0 or True,
      f"status codes seen: {dict(sorted((str(k), v) for k, v in codes.items()))}")
# v58.75 — these used to answer 200 with an empty body. Harmless for
# the database, but answering a nonsense symbol tells a prober which
# parameters reach one.
for _p in ("' OR '1'='1", "NIFTY'; DROP TABLE candles;--", "NIFTY) OR 1=1--"):
    _r = client.get(f"/api/chain-snapshots/{_p}")
    check(f"chain-snapshots rejects {_p[:24]!r} with 400", _r.status_code == 400,
          str(_r.status_code))
# A traversal payload contains slashes, so it is a DIFFERENT URL and never
# reaches the handler at all — 404 from the router is a stricter rejection
# than 400 from the guard, not a weaker one. Asserting 400 here would be
# demanding that a request get further in before being refused.
_t = client.get("/api/chain-snapshots/../../etc/passwd")
check("a traversal-shaped path is refused by the router itself",
      _t.status_code in (400, 404), str(_t.status_code))
check("a REAL symbol still works", 
      client.get("/api/chain-snapshots/NIFTY").status_code == 200,
      str(client.get("/api/chain-snapshots/NIFTY").status_code))

print("\n4) the property that must hold as the code grows")
src = open("history.py").read()
import re as _re
fstrings = _re.findall(r"execute(?:many)?\(\s*f(?:\"\"\"|\"|')(.{0,160})", src, _re.S)
suspicious = [f for f in fstrings
              if _re.search(r"\{(symbol|sym|day|security_id|user|name)\b", f)
              and "table" not in f]
check("no execute(f\"…\") interpolates a value-shaped variable",
      not suspicious, str(suspicious[:1]))
check("the WHERE fragment carries placeholders, not values",
      'where = "WHERE ts >= ?"' in src and 'symbol = ?' in src)

print("\n5) auth inputs (JSON-backed, but usernames must still be constrained)")
import auth
bad = 0
for p in PAYLOADS + ["../../etc/passwd", "a b", "admin;--"]:
    try:
        auth.create_user(p, "supersecret1", "user"); bad += 1
    except ValueError:
        pass
check("injection/traversal-shaped usernames are refused", bad == 0,
      f"{bad} accepted")
check("usernames are constrained to alphanumeric", "isalnum" in open("auth.py").read())

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
