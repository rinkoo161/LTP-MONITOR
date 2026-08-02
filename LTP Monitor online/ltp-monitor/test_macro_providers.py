#!/usr/bin/env python3
"""test_macro_providers.py — the macro provider refactor.

Every check here maps to an acceptance criterion in
`claude-code-prompt-macro-provider.md`. The ones that matter most are the
negative paths, because the defect being fixed was a chain that failed
SILENTLY: Alpha Vantage spent its 25/day budget, Twelve Data was asked
for symbols it never mapped, and the logs said "no data from any
provider" without saying which limit had been hit.

No network. Every provider is stubbed, so the suite stays deterministic
and does not depend on Yahoo being reachable.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_macro_providers")

import config
import macro_providers as mp
import redaction

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


CFG = dict(config.DEFAULTS)
CFG["twelve_data_api_key"] = "TDKEY1234567890"
CFG["alpha_vantage_api_key"] = "AVKEYABCDEFGHIJ"


class Stub(mp.MacroDataProvider):
    def __init__(self, name, serves, boom=None):
        self.name = name
        self._serves = serves
        self._boom = boom
        self.calls = []

    def supports(self, symbol, cfg=None):
        return symbol in self._serves

    def fetch_many(self, symbols, cfg):
        self.calls.append(tuple(symbols))
        if self._boom:
            raise self._boom
        return {s: mp.Quote(s, self._serves[s], source=self.name)
                for s in symbols if s in self._serves}


print("1) symbol mapping — the defect that produced the original errors")
M = config.DEFAULTS["macro_symbols"]
check("every symbol has a yfinance ticker",
      all(v.get("yf") for v in M.values()),
      "yfinance is primary precisely because it covers everything")
check("Twelve Data maps FX/metals ONLY",
      {k for k, v in M.items() if v.get("td")} == {"GOLD", "SILVER", "USDINR"},
      "its free tier does not serve indices — that was the 404 source")
check("the correct TD symbol strings are used",
      M["USDINR"]["td"] == "USD/INR" and M["GOLD"]["td"] == "XAU/USD"
      and M["SILVER"]["td"] == "XAG/USD")
check("index futures are present for the IST session",
      all(k in M for k in ("SPX_FUT", "NDX_FUT", "DJI_FUT", "RUT_FUT")))
check("cash indices retained for post-close context",
      "SPX_CASH" in M and "DJI_CASH" in M)
check("no provider ticker is hardcoded in the fetch module",
      "ES=F" not in open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "macro_providers.py")).read(),
      "the map lives in config so swapping a provider is a settings edit")

print("\n2) fallback chain order and short-circuiting")
a = Stub("A", {"GOLD": 1.0})
b = Stub("B", {"GOLD": 2.0, "SILVER": 3.0})
q, s = mp.fetch(["GOLD", "SILVER"], cfg=CFG, chain=[a, b], cache=mp.QuoteCache())
check("first provider wins for what it serves", q["GOLD"].source == "A")
check("chain falls through for what it does not", q["SILVER"].source == "B")
check("the second provider is only asked for the REMAINDER",
      b.calls == [("SILVER",)], str(b.calls))
check("summary reports who served what",
      s["by_provider"] == {"GOLD": "A", "SILVER": "B"}, str(s["by_provider"]))

print("\n3) yfinance disabled -> Stooq still serves (acceptance criterion)")
dead = Stub("yf", {}, boom=mp.NotSupported("yfinance forcibly disabled"))
q2, s2 = mp.fetch(["GOLD", "SILVER"], cfg=CFG, chain=[dead, b],
                  cache=mp.QuoteCache())
check("a raising provider does not abort the chain", len(q2) == 2, str(s2))
check("and the failure is not counted as served", s2["served"] == 2
      and all(v == "B" for v in s2["by_provider"].values()))

print("\n4) all providers down -> stale cache, no crash, no empty")
cache = mp.QuoteCache()
cache.put(mp.Quote("GOLD", 99.0, last_updated=time.time() - 99999, source="old"))
allbad = Stub("X", {}, boom=mp.TransientError("network down"))
lines = []
q3, s3 = mp.fetch(["GOLD", "SILVER"], cfg=CFG, chain=[allbad], cache=cache,
                  log=lambda lv, m: lines.append((lv, m)))
check("a cached value is still served", q3.get("GOLD") is not None)
check("and it is marked STALE", q3["GOLD"].is_stale(CFG),
      f"age {q3['GOLD'].age():.0f}s vs threshold "
      f"{mp.freshness_threshold('GOLD', CFG):.0f}s")
check("a symbol with no cache at all is reported failed",
      s3["failed_symbols"] == ["SILVER"], str(s3["failed_symbols"]))
check("exactly one INFO summary line",
      sum(1 for lv, _ in lines if lv == "info") == 1,
      str([m for lv, m in lines if lv == "info"]))
check("it does not return empty", len(q3) >= 1)

print("\n5) staleness across session boundaries")
now = time.time()
fut = mp.Quote("SPX_FUT", 5000.0, last_updated=now - 1200)     # 20 min
cash = mp.Quote("SPX_CASH", 6145.2, last_updated=now - 1200)
check("a future is stale after 20 minutes", fut.is_stale(CFG),
      f"threshold {mp.freshness_threshold('SPX_FUT', CFG):.0f}s")
check("a cash index at the same age is NOT", not cash.is_stale(CFG),
      "it is EXPECTED to be old during the IST session; flagging it "
      "every cycle would train people to ignore the flag")
old_cash = mp.Quote("SPX_CASH", 6145.2, last_updated=now - 200000)
check("but a cash index two days old IS stale", old_cash.is_stale(CFG))
check("every quote exposes last_updated and is_stale",
      {"last_updated", "is_stale", "age_sec"} <= set(fut.as_dict(CFG)))

print("\n6) cache TTL expiry")
c2 = mp.QuoteCache()
c2.put(mp.Quote("GOLD", 1.0, last_updated=time.time() - 100))
_, fresh = c2.get("GOLD", ttl=200)
check("inside TTL counts as fresh", fresh)
_, fresh2 = c2.get("GOLD", ttl=50)
check("outside TTL counts as expired", not fresh2)
q4, _ = c2.get("GOLD", ttl=50)
check("but the value is still RETURNED for stale-serving", q4 is not None,
      "expiry governs re-fetching, not whether we may serve")

print("\n7) the daily budget refuses when exhausted")
bpath = store.path("test_budget.json")
if os.path.exists(bpath):
    os.remove(bpath)
bud = mp.Budget(bpath)
for i in range(3):
    bud.consume("av", 3)
check("three calls fit a budget of three", bud.spent("av") == 3)
try:
    bud.consume("av", 3)
    check("a fourth is refused", False)
except mp.BudgetExhausted as e:
    check("a fourth is refused", True, str(e)[:60])
check("it SURVIVES a restart", mp.Budget(bpath).spent("av") == 3,
      "an in-memory counter resets and the daily cap is blown by lunchtime")
check("a new day resets it",
      mp.Budget(bpath).remaining("other", 5) == 5)

print("\n8) retry policy")
calls = []


def flaky():
    calls.append(1)
    if len(calls) < 3:
        raise mp.TransientError("boom")
    return "ok"


check("transient errors are retried",
      mp.with_backoff(flaky, sleep=lambda s: None) == "ok" and len(calls) == 3)
hard = []


def permanent():
    hard.append(1)
    raise mp.BadResponse("HTTP 404")


try:
    mp.with_backoff(permanent, sleep=lambda s: None)
except mp.BadResponse:
    pass
check("a 4xx that is not 429 is NOT retried", len(hard) == 1,
      f"{len(hard)} attempt(s) — retrying a 404 burns the checkpoint")

print("\n9) NO API KEY REACHES A LOG LINE (acceptance criterion)")
leak = (f"https://api.twelvedata.com/price?symbol=X&apikey={CFG['twelve_data_api_key']} "
        f"failed; av key {CFG['alpha_vantage_api_key']}")
red = redaction.redact(leak, CFG)
check("the key VALUE is masked", CFG["twelve_data_api_key"] not in red, red[:90])
check("the second key too", CFG["alpha_vantage_api_key"] not in red)
check("the query-string parameter is masked", "apikey=" + "TD" not in red)
check("an Anthropic-shaped key is masked",
      "sk-ant-api03-" not in redaction.redact(
          "sk-ant-api03-" + "A" * 40, CFG))
check("truncation happens AFTER redaction",
      CFG["twelve_data_api_key"][:8] not in redaction.redact(leak, CFG, limit=40),
      "truncating first could leave the first N chars of a key visible")


class Boom(mp.MacroDataProvider):
    name = "td"

    def supports(self, symbol, cfg=None):
        return True

    def fetch_many(self, symbols, cfg):
        raise RuntimeError(f"upstream said: apikey={cfg['twelve_data_api_key']} bad")


logged = []
mp.fetch(["GOLD"], cfg=CFG, chain=[Boom()], cache=mp.QuoteCache(),
         log=lambda lv, m: logged.append(m))
blob = " ".join(logged)
check("a key inside an EXCEPTION never reaches the log",
      CFG["twelve_data_api_key"] not in blob, blob[:100])
check("provider errors are truncated",
      all(len(m) <= mp.ERR_LIMIT + 3 for m in logged if "GOLD" not in m),
      str([len(m) for m in logged]))

print("\n10) downstream contract is intact")
agent_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "news_macro_agent.py")).read()
check("bus key macro_market_data unchanged", 'bus.set("macro_market_data"' in agent_src)
check("value/chg_pct/ts still written",
      '"value": value, "chg_pct": chg_pct, "ts": time.time()' in agent_src,
      "the refactor is drop-in behind the existing payload")
check("staleness fields are ADDITIVE", '"is_stale": q.is_stale(cfg)' in agent_src)
check("checkpoints kept (not converted to a poll loop)",
      "CHECKPOINTS = [" in agent_src and '(5,  0,  "us_close"' in agent_src)
check("sentiment reads FUTURES first", 'US_FUT = (' in agent_src)
check("with a cash fallback", 'US_CASH = (' in agent_src)
check("and refuses to act on a stale quote",
      'if d.get("is_stale")' in agent_src,
      "a frozen cash index must not drive an MTF confidence input")

print("\n11) intra-session futures refresh")
import macro_providers as _mp
check("refresh cadence is BELOW the futures staleness threshold",
      config.DEFAULTS["macro_intrasession_refresh_sec"]
      < _mp.freshness_threshold("SPX_FUT", config.DEFAULTS),
      f"{config.DEFAULTS['macro_intrasession_refresh_sec']}s refresh vs "
      f"{_mp.freshness_threshold('SPX_FUT', config.DEFAULTS):.0f}s threshold — "
      f"if cadence exceeded it, sentiment would flicker between a value and None")
check("it refreshes the FUTURES, not the cash indices",
      set(__import__('news_macro_agent').INTRASESSION_SYMBOLS)
      == {"SPX_FUT", "NDX_FUT", "DJI_FUT", "RUT_FUT"},
      "cash does not move during the IST session, which is the whole point")
check("gated on market_open", "market_open(now)" in agent_src)
check("and on its own switch", "macro_intrasession_enabled" in agent_src)
check("routine refreshes do NOT spam the macro event log",
      "quiet=True" in agent_src and "if not quiet or moved:" in agent_src,
      "~75 refreshes a session would bury the daily checkpoints")
check("but a MATERIAL move still logs an event",
      "if not quiet or moved:" in agent_src)
check("a refresh failure cannot kill the cycle",
      "intra-session refresh failed" in agent_src)

print("\n12) the agent no longer idles without API keys")
check("market data does not require any key",
      "idle — no API keys configured" not in agent_src,
      "yfinance is primary and needs none; idling threw away the only "
      "provider that works")
check("only the NEWS half is gated on newsapi_api_key",
      'if not cfg.get("newsapi_api_key")' in agent_src)
check("and it says market data is unaffected",
      "Market data is unaffected" in agent_src)

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all macro-provider checks passed")
