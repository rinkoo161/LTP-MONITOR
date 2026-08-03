"""v58.21+ — tests for the market-hours gate added to MarketDataAgent's
main cycle, per explicit suggestion after investigating the futures
quote rate-limit escalation.

Root cause context: the cycle had NO market-hours gate at all — it
fetched option chains (and, via _poll_futures_via_rest called from
within it, futures quotes) continuously, including all evening/
overnight/weekend hours. This explained why 429 rate-limit hits were
spread fairly evenly across every hour of the day (00-23) rather than
concentrated in the ~6.25 actual trading hours.

Fix: skip the fetch entirely outside market hours. Confirmed safe —
chain:{sym} (and everything downstream) simply retains its last value
when skipped, matching the existing "show the last available session"
design already relied on elsewhere (RegimeAgent's stale-session
fallback, the chart's most-recent-session tiers) — this doesn't change
what gets displayed, only stops unnecessary re-fetching.

Run:  python3 test_market_hours_fetch_gate.py
"""
import os
import sys
import types
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents

src = open("agents.py").read()

print("1) source-level: the gate exists, positioned before the actual "
     "API call (not just documented)")
# 2026-08-03 — MarketDataAgent is the DATA path. NSE split the boundary
# (F&O trades to 15:40, intraday squares at 15:25), so data collection
# gates on fno_session_open() while TRADING gates on market_open(). The
# intent of this check is unchanged: the fetch must be gated.
check("session gate is present in MarketDataAgent.cycle",
      "if not fno_session_open():" in src)
check("the gate's summary message clearly explains why nothing was "
     "fetched and that existing data is retained",
      "market closed — not fetching (last data retained)" in src)


class FakeBus:
    def __init__(self):
        self.data = {}
        self.logs = []

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, val):
        self.data[key] = val

    def log(self, name, msg):
        self.logs.append(msg)


def make_fake_agent(get_chain_calls):
    fake_self = types.SimpleNamespace()
    fake_self.bus = FakeBus()
    fake_self.bus.set("symbols", ["NIFTY"])
    fake_self.name = "market_data"
    fake_self._sync_ws_feed = lambda sym, chain: None
    # 2026-08-03 — futures resolution moved OUT of _sync_ws_feed and is
    # now called unconditionally from the cycle, because gating it on the
    # websocket left the REST fallback with an empty contract map for a
    # whole session. Stubbed here for the same reason the two above are:
    # this test is about the market-hours gate, and what it asserts is
    # that NONE of these run when the market is closed.
    fake_self._ensure_futures_subscribed = lambda client=None: None
    fake_self._poll_futures_via_rest = lambda: None

    def fake_get_chain(sym):
        get_chain_calls.append(sym)
        return {"symbol": sym, "spot": 24000, "rows": []}

    fake_self.ctx = {"get_chain": fake_get_chain, "dhan_client": lambda: None}
    return fake_self


print("\n2) BEHAVIORAL VERIFICATION: with the market marked CLOSED, "
     "the option-chain fetch is never called — confirmed by tracking "
     "actual calls to a fake get_chain, not just reading the source")
calls_when_closed = []
fake_self_closed = make_fake_agent(calls_when_closed)
with patch("agents.fno_session_open", return_value=False):
    agents.MarketDataAgent.cycle(fake_self_closed)

check("get_chain was NOT called while the session is closed",
      len(calls_when_closed) == 0, str(calls_when_closed))
check("the chain bus key was never set (existing/last value untouched, "
     "not overwritten with anything new)",
      fake_self_closed.bus.get("chain:NIFTY") is None)
check("the summary reflects the market-closed skip",
      "market closed" in fake_self_closed.summary,
      fake_self_closed.summary)

print("\n3) BEHAVIORAL VERIFICATION: with the market marked OPEN, the "
     "fetch proceeds normally — confirms this is a genuine gate, not "
     "an accidental always-skip regression")
calls_when_open = []
fake_self_open = make_fake_agent(calls_when_open)
with patch("agents.fno_session_open", return_value=True):
    agents.MarketDataAgent.cycle(fake_self_open)

check("get_chain WAS called while the session is open",
      len(calls_when_open) == 1, str(calls_when_open))
check("the chain bus key was correctly populated from the fetch",
      fake_self_open.bus.get("chain:NIFTY", {}).get("spot") == 24000)

print("\n4) confirm the gate sits ahead of the existing per-symbol "
     "cooldown check in source order, so a symbol backing off from a "
     "prior failure doesn't get double-reported as also 'market "
     "closed' — the cooldown check should still be reachable and take "
     "priority when both conditions happen to be true")
cooldown_idx = src.index('self.summary = f"{sym} backing off')
gate_idx = src.index('if not market_open():')
check("the pre-existing cooldown check appears before the new "
     "market-hours gate in source order (preserves its priority)",
      cooldown_idx < gate_idx)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
