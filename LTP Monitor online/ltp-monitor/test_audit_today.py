"""v58.9 (part 6, item 6) — tests for backtester.audit_today(), the
retroactive candle-by-candle "learn and adopt" audit requested
multiple times: replay the exact strategy rules against today's own
archived data, then compare against what actually happened live,
surfacing genuine gaps (rule-valid setups live never took; live trades
the rules wouldn't have made) rather than a single aggregate number.

Reuses backtester's own _replay_for()/get_params()/metrics() (all
already existed from the v56 optimizer) — days=[today] scoping already
worked on all three replay_* functions, no changes needed there.

Run:  python3 test_audit_today.py
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agents
import backtester
import history

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


def candles(closes, t0, step=60, wick=1.5):
    return [{"time": t0 + i * step, "open": c, "high": c + wick, "low": c - wick,
             "close": c} for i, c in enumerate(closes)]


def cross_up_series(n=200, base=23800.0):
    out = []
    for i in range(n - 12):
        out.append(base - i * 2 + math.sin(i / 5) * base * 0.004)
    for i in range(12):
        out.append(out[-1] + 14)
    return out


def seed_symbol(sym):
    conn = history._conn()
    conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{sym}%",))
    conn.execute("DELETE FROM instruments WHERE symbol=?", (sym,))
    conn.execute("INSERT INTO instruments (security_id, symbol, kind) VALUES (?,?,?)",
                (f"{sym}_IDX", sym, "idx"))
    conn.commit()
    today = agents.now_ist().strftime("%Y-%m-%d")
    base_ts = int(time.mktime(time.strptime(today, "%Y-%m-%d"))) + 9 * 3600 + 15 * 60
    full = cross_up_series(200)
    history.upsert_candles(f"{sym}_IDX", [
        {"ts": c["time"], "o": c["open"], "h": c["high"], "l": c["low"],
        "c": c["close"], "v": None, "oi": None} for c in candles(full, base_ts)])
    return today


def cleanup(sym):
    conn = history._conn()
    conn.execute("DELETE FROM candles WHERE security_id LIKE ?", (f"{sym}%",))
    conn.execute("DELETE FROM instruments WHERE symbol=?", (sym,))
    conn.commit()


FIXED_PARAMS = {"fast": 5, "slow": 13, "mtf_confirm": 0, "max_trades_per_day": 99}
real_get_params = backtester.get_params
backtester.get_params = lambda name, symbol=None: FIXED_PARAMS

try:
    print("1) no real trades at all today: every backtest-found setup "
         "is correctly classified as missed_by_live")
    SYM1 = "AUDITTEST_A"
    today1 = seed_symbol(SYM1)
    try:
        r1 = backtester.audit_today("ema_mtf", SYM1, real_trades=[])
        check("backtest found real trades to compare against (fixture sanity)",
              len(r1["backtest_trades"]) > 0, str(len(r1["backtest_trades"])))
        check("zero matched (nothing real to match against)",
              len(r1["matched"]) == 0)
        check("all backtest trades classified as missed_by_live",
              len(r1["missed_by_live"]) == len(r1["backtest_trades"]),
              f"{len(r1['missed_by_live'])} vs {len(r1['backtest_trades'])}")
        check("zero unexpected_in_live (no real trades exist)",
              len(r1["unexpected_in_live"]) == 0)
        check("gap_summary correctly reports both trade counts",
              str(len(r1["backtest_trades"])) in r1["gap_summary"] and "real: 0" in r1["gap_summary"],
              r1["gap_summary"])
    finally:
        cleanup(SYM1)

    print("\n2) a real trade closely matching a backtest entry time (within "
         "the 5-minute tolerance) is correctly MATCHED, not counted as "
         "missed or unexpected")
    SYM2 = "AUDITTEST_B"
    today2 = seed_symbol(SYM2)
    try:
        preview = backtester.audit_today("ema_mtf", SYM2, real_trades=[])
        first_entry = preview["backtest_trades"][0]["entry_ts"]
        real_trades = [{"closed_date": today2, "symbol": SYM2, "strategy": "ema_mtf",
                        "opened_ts": first_entry + 45, "pnl": 500}]
        r2 = backtester.audit_today("ema_mtf", SYM2, real_trades=real_trades)
        check("exactly one match found",
              len(r2["matched"]) == 1, str(len(r2["matched"])))
        check("the remaining backtest trades are missed_by_live "
              "(total - 1 matched)",
              len(r2["missed_by_live"]) == len(preview["backtest_trades"]) - 1,
              f"{len(r2['missed_by_live'])} vs {len(preview['backtest_trades']) - 1}")
        check("zero unexpected (the one real trade was consumed by the match)",
              len(r2["unexpected_in_live"]) == 0)
    finally:
        cleanup(SYM2)

    print("\n3) a real trade with NO nearby backtest entry (way outside "
         "tolerance) is correctly classified as unexpected_in_live")
    SYM3 = "AUDITTEST_C"
    today3 = seed_symbol(SYM3)
    try:
        preview3 = backtester.audit_today("ema_mtf", SYM3, real_trades=[])
        first_entry3 = preview3["backtest_trades"][0]["entry_ts"]
        real_trades3 = [{"closed_date": today3, "symbol": SYM3, "strategy": "ema_mtf",
                         "opened_ts": first_entry3 + 100000, "pnl": -300}]
        r3 = backtester.audit_today("ema_mtf", SYM3, real_trades=real_trades3)
        check("the real trade is classified as unexpected, not matched",
              len(r3["unexpected_in_live"]) == 1 and len(r3["matched"]) == 0,
              f"unexpected={len(r3['unexpected_in_live'])} matched={len(r3['matched'])}")
        check("ALL backtest trades remain missed_by_live (none consumed "
              "by a false match)",
              len(r3["missed_by_live"]) == len(preview3["backtest_trades"]),
              f"{len(r3['missed_by_live'])} vs {len(preview3['backtest_trades'])}")
    finally:
        cleanup(SYM3)

    print("\n4) real trades are correctly filtered by day/symbol/strategy "
         "— a trade for a DIFFERENT symbol, day, or strategy must not "
         "leak into the comparison")
    SYM4 = "AUDITTEST_D"
    today4 = seed_symbol(SYM4)
    try:
        wrong_symbol = [{"closed_date": today4, "symbol": "SOMEOTHERSYM",
                        "strategy": "ema_mtf", "opened_ts": 1, "pnl": 100}]
        wrong_day = [{"closed_date": "2020-01-01", "symbol": SYM4,
                     "strategy": "ema_mtf", "opened_ts": 1, "pnl": 100}]
        wrong_strategy = [{"closed_date": today4, "symbol": SYM4,
                          "strategy": "bear_call_spread", "opened_ts": 1, "pnl": 100}]
        r4 = backtester.audit_today("ema_mtf", SYM4,
                                    real_trades=wrong_symbol + wrong_day + wrong_strategy)
        check("none of the mismatched trades leaked into real_trades",
              len(r4["real_trades"]) == 0, str(len(r4["real_trades"])))
    finally:
        cleanup(SYM4)

    print("\n5) source-level guard: audit_today reuses the existing "
         "replay dispatch and params lookup rather than reimplementing "
         "strategy logic a third time")
    src = open("backtester.py").read()
    check("audit_today calls the shared _replay_for(), not a new replay "
          "implementation",
          "bt_trades = _replay_for(name, symbol, params, days=[today])" in src)
    check("audit_today calls the shared get_params(), same source of "
          "truth the live system and the optimizer both use",
          "params = get_params(name, symbol)" in src)
    check("audit_today calls the shared metrics(), not a parallel P&L "
          "aggregation",
          "bt_metrics = metrics(bt_trades)" in src)

    print("\n6) API endpoint: full detail available on demand (not just "
         "the trimmed daily-journal summary)")
    from fastapi.testclient import TestClient
    import app as app_module
    SYM5 = "AUDITTEST_E"
    today5 = seed_symbol(SYM5)
    try:
        client = TestClient(app_module.app)
        r5 = client.get(f"/api/backtest/audit-today?symbol={SYM5}&name=ema_mtf")
        d5 = r5.json()
        check("endpoint returns ok:True with real backtest trades",
              d5.get("ok") is True and len(d5.get("backtest_trades", [])) > 0,
              str(d5.get("ok")))
        check("full detail present (backtest_trades list, not just a summary)",
              "backtest_trades" in d5 and "matched" in d5 and "missed_by_live" in d5)
    finally:
        cleanup(SYM5)

    print("\n7) source-level guard: the daily LearningAgent cycle "
         "actually wires this in, trimmed to avoid bloating the "
         "already-large journal.json file further")
    agents_src = open("agents.py").read()
    check("LearningAgent's cycle calls audit_today for each traded pairing",
          "audits[f\"{sym}:{name}\"] = {" in agents_src and
          "full = backtester.audit_today(" in agents_src)
    check("the persisted journal entry includes daily_audit",
          '"daily_audit": audits}' in agents_src)
    check("the persisted version is trimmed (summary/count fields "
          "only) rather than embedding the full backtest_trades list "
          "directly in the persisted dict",
          '"backtest_trade_count": len(full["backtest_trades"])' in agents_src and
          '"backtest_trades": full["backtest_trades"]' not in agents_src)

finally:
    backtester.get_params = real_get_params

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
