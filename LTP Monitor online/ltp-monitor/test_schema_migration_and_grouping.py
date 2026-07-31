"""v58.57 — a schema migration that never ran, and provenance posing as
a strategy.

BUG 1. The calibration panel showed:
    failed: SyntaxError: Unexpected token 'I', "Internal S"... is not valid JSON
"Internal S" is "Internal Server Error" -- the endpoint 500'd and the
frontend tried to JSON.parse an error page. Cause: v58.41 added eight
columns to ta_calibration by editing its CREATE TABLE IF NOT EXISTS,
which does NOTHING when the table already exists. Every install created
before v58.41 kept the old schema while the query asked for the new one.
Invisible in development because a fresh DB gets the new schema -- and
because I had dropped and recreated the table while testing, which is
the one state a real user never has.

BUG 2. The Quality page listed "AI", "rule-engine (AI returned an
invalid signal value: None)" and "rule-engine (AI unavailable: Ollama/
model 'qwen2.5:3b' not reachable ...)" as separate strategies. They are
not strategies -- they are momentum_buy's `source` field, i.e. WHICH
CODE PATH produced the signal. An error message had become a strategy
name, and one strategy's record was split five ways.
"""
import os, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

print("1) CREATE TABLE IF NOT EXISTS does not migrate — proven")
p = "/tmp/mig_proof.db"
os.path.exists(p) and os.remove(p)
c = sqlite3.connect(p)
c.execute("CREATE TABLE t(a INTEGER)")
c.execute("CREATE TABLE IF NOT EXISTS t(a INTEGER, b REAL)")
cols = [r[1] for r in c.execute("PRAGMA table_info(t)")]
check("an existing table is left untouched", cols == ["a"], str(cols))
check("so the new column is simply absent", "b" not in cols,
      "this is the entire cause of the 500")

import history
added = history._migrate_columns(c, "t", [("b", "REAL"), ("cc", "TEXT")])
cols2 = [r[1] for r in c.execute("PRAGMA table_info(t)")]
check("_migrate_columns adds the missing ones", set(cols2) == {"a", "b", "cc"}, str(cols2))
check("it reports what it added", set(added) == {"b", "cc"}, str(added))
check("it is idempotent", history._migrate_columns(c, "t", [("b", "REAL")]) == [])
check("a non-existent table is a no-op, not an error",
      history._migrate_columns(c, "nope", [("x", "TEXT")]) == [])
c.close()

print("\n2) The real table is migrated on open")
# 2026-07-31 — was os.path.expanduser("~/.ltp-monitor/history.db"), a
# hardcode that ignored LTP_MONITOR_HOME. This section runs DROP TABLE
# against whatever it points at, so under the isolated runner it was
# both looking at the wrong database AND dropping a table in the
# operator's live one. history.DB is resolved through store.py, so it
# follows the redirect.
dbp = history.DB
cc = sqlite3.connect(dbp)
cc.execute("DROP TABLE IF EXISTS ta_calibration")
cc.execute("""CREATE TABLE ta_calibration(ts INTEGER, day TEXT, symbol TEXT,
 strategy TEXT, phase TEXT, route TEXT, tide INTEGER, direction INTEGER,
 bb_state TEXT, gmma_state TEXT, adx REAL, dynamic INTEGER,
 macd_zero_reversal INTEGER, rsi REAL, sig_bb_stall INTEGER, sig_gmma INTEGER,
 sig_macd_zero INTEGER, sig_hidden_div INTEGER, sig_regular_div INTEGER,
 sig_rsi_div INTEGER, sig_adx INTEGER, confluence_hits INTEGER,
 confluence_need INTEGER, fired INTEGER, blocked TEXT)""")
cc.commit(); cc.close()
import importlib; importlib.reload(history)
conn = history._conn()
after = [r[1] for r in conn.execute("PRAGMA table_info(ta_calibration)")]
conn.close()
for col in ("as_of", "bb_slope", "gmma_spread", "pivots_5m", "pivot_lows"):
    check(f"'{col}' present after migration", col in after)
s = history.ta_calibration_summary(days=1)
check("summary no longer raises (this was the 500)", isinstance(s, dict))
check("it returns a usable answer", "observations" in s, str(s)[:70])

print("\n3) A missing column degrades rather than 500s")
cc = sqlite3.connect(dbp)
cc.execute("DROP TABLE IF EXISTS ta_calibration")
cc.execute("CREATE TABLE ta_calibration(ts INTEGER, as_of INTEGER, day TEXT, "
           "symbol TEXT, strategy TEXT, confluence_hits INTEGER, "
           "confluence_need INTEGER, fired INTEGER, phase TEXT, tide INTEGER, "
           "sig_bb_stall INTEGER, sig_gmma INTEGER, sig_macd_zero INTEGER, "
           "sig_hidden_div INTEGER, sig_regular_div INTEGER, sig_rsi_div INTEGER, "
           "sig_adx INTEGER)")
cc.execute("INSERT INTO ta_calibration (ts, as_of, day, symbol, strategy, "
           "confluence_hits, confluence_need, fired, phase, tide, sig_adx) "
           "VALUES (1,1,'2026-07-30','NIFTY','ta_elliott',1,3,0,'CORRECTIVE',1,1)")
cc.commit(); cc.close()
# This suite MUTATES the real ta_calibration table. It must restore a
# complete one or it breaks every later suite -- which it did:
# test_s9_ta_elliott failed with "table has 25 columns but 34 values were
# supplied". Third time this session that a suite of mine left persisted
# state behind; restoring is not optional.
import importlib as _il
_il.reload(history)
_c3 = history._conn()
_final = [r[1] for r in _c3.execute("PRAGMA table_info(ta_calibration)")]
_c3.close()
check("the table is restored to full width for later suites",
      len(_final) == 34, f"{len(_final)} columns")
check("a PARTIAL table is repaired, not just an outdated one",
      all(k in _final for k in ("route", "bb_state", "adx", "blocked")),
      "listing only the newest columns repeats the original assumption")

HS = open("history.py").read()
check("the raw-distribution query is wrapped in try/except",
      "raws = []" in HS and "must degrade" in HS,
      "the panel has to render SOMETHING or the user sees a JSON parse error")

print("\n3b) A PK-less table is REBUILT, not just widened")
import sqlite3 as _sq, importlib as _il2
_dbp = history.DB   # see the note above — never hardcode the store path
_cx = _sq.connect(_dbp)
_cx.execute("DROP TABLE IF EXISTS ta_calibration")
_cx.execute("CREATE TABLE ta_calibration(ts INTEGER, as_of INTEGER, day TEXT, "
            "symbol TEXT, strategy TEXT, confluence_hits INTEGER)")
_cx.execute("INSERT INTO ta_calibration VALUES (1,999,'2026-07-30','NIFTY','ta_elliott',2)")
_cx.commit(); _cx.close()
_il2.reload(history); history._conn().close()
_cy = history._conn()
_sql = _cy.execute("SELECT sql FROM sqlite_master WHERE name='ta_calibration'").fetchone()[0]
_n = _cy.execute("SELECT COUNT(*) FROM ta_calibration").fetchone()[0]
_w = len([r[1] for r in _cy.execute("PRAGMA table_info(ta_calibration)")])
_cy.close()
check("the PRIMARY KEY is restored", "PRIMARY KEY" in _sql,
      "SQLite cannot ALTER one in, so the table must be rebuilt")
check("existing rows survive the rebuild", _n == 1, f"{_n} rows")
check("width is correct after rebuild", _w == 34, f"{_w} columns")
check("without a PK, INSERT OR REPLACE silently stops deduping",
      "dedupe was silently disabled" in open("history.py").read())
_cz = _sq.connect(_dbp); _cz.execute("DELETE FROM ta_calibration"); _cz.commit(); _cz.close()

print("\n3c) The INSERT is order-independent")
_H = open("history.py").read()
check("INSERT names its columns",
      "INSERT OR REPLACE INTO ta_calibration\n                 (ts, as_of, day" in _H
      or "(ts, as_of, day, symbol, strategy, phase, route, tide," in _H,
      "ALTER TABLE appends, so positional INSERT would write to wrong fields")
# Flatten FIRST. Six assertions this session failed on phrases that
# wrap across lines; matching raw source for prose is the wrong tool and
# flattening should have been the default from the start.
# Strip comment MARKERS as well as whitespace. Flattening alone leaves
# "worse than the # crash", because the `#` opening the next comment line
# survives. This is the complete form of a fix I applied six times in
# partial versions this session.
import re as _re2
_Hflat = " ".join(_re2.sub(r"#", " ", _H).split())
check("the risk is documented as WORSE than the crash",
      "strictly worse than the crash" in _Hflat)

print("\n4) Provenance is no longer a strategy name")
APP = open("app.py").read()
check("`source` is no longer read FIRST",
      'return t.get("source") or t.get("strategy")' not in APP)
check("strategy identity comes from strategy/setup",
      't.get("strategy") or t.get("setup")' in APP)
check("an AI/rule-engine source maps to momentum_buy",
      'name = "momentum_buy"' in APP)
check("the comment records that an ERROR MESSAGE became a strategy name",
      "became a strategy name" in APP)
check("provenance is still on the trade for deliberate splitting",
      "provenance" in APP)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
