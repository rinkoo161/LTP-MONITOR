"""v58.20+ — tests for the futures REST quote poll rate-limit fix.

Root cause investigation, per direct request to look into the futures
exit-criteria slippage: found a genuinely severe, escalating pattern
of 429 rate-limit errors on Dhan's /marketfeed/quote endpoint (28 on
07-16 -> 1,151 on 07-28), each triggering a 60s+ blackout with no
fresh futures price during that window — directly explaining a real
~10.4-point stop-loss slippage found in trades.jsonl (SL configured at
26239.64, actual exit at 26250.0).

The pacing guard (2.5s gap) was ALREADY inside Dhan's documented 1
req/s ceiling for this specific caller — yet errors kept climbing
anyway, the real tell that something else (most plausibly the much
more frequent option-chain fetch traffic, if Dhan enforces rate limits
at the account level rather than strictly per-endpoint) is very
plausibly sharing whatever the true budget is. Not able to confirm
Dhan's exact policy from this environment, so this fix is deliberately
the safe, concrete half rather than a diagnosis dressed up as certain:
widen this caller's own pacing further (2.5s -> 4.0s) and make backoff
recovery more deliberate (3 consecutive successes required before
fully clearing the escalation, instead of resetting on the very first
lucky success — which could otherwise immediately drop back to a short
retry only to fail again right away).

Run:  python3 test_futures_quote_ratelimit_fix.py
"""
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import agents

src = open("agents.py").read()

print("1) source-level: the pacing gap was actually widened, not just "
     "documented as widened")
check("the old 2.5s gap check is gone",
      "if time.time() - last < 2.5:" not in src)
check("the new 4.0s gap check is present",
      "if time.time() - last < 4.0:" in src)

print("\n2) source-level: backoff recovery now requires multiple "
     "consecutive successes, not just one")
check("a _futures_success_streak counter is tracked",
      "_futures_success_streak" in src)
check("the streak only clears once successes reach 3",
      "if successes >= 3:" in src)
check("any failure resets the success-streak counter (so a single "
     "success amid ongoing failures can't slowly count up to 3 "
     "without genuinely recovering)",
      "self._futures_success_streak = 0" in src)


class FakeBus:
    def __init__(self):
        self.logs = []

    def log(self, name, msg):
        self.logs.append(msg)

    def get(self, key, default=None):
        return default

    def set(self, key, val):
        pass


class FakeDhanClient:
    def __init__(self, fail_count):
        self.fail_count = fail_count
        self.calls = 0

    def quote_batch(self, seg_map):
        self.calls += 1
        if self.calls <= self.fail_count:
            raise Exception("429 Client Error: Too Many Requests for url: test")
        return {}


def make_fake_agent(fail_count):
    fake_self = types.SimpleNamespace()
    fake_self.bus = FakeBus()
    fake_self._future_roles = {"NIFTY": "primary"}
    fake_self._future_sec_ids = {12345: "NIFTY"}
    fake_client = FakeDhanClient(fail_count=fail_count)
    fake_self.ctx = {"dhan_client": lambda: fake_client}
    fake_self.name = "market_data"
    fake_self._last_quote_batch_call = 0
    fake_self._quote_batch_fail_until = 0
    return fake_self, fake_client


print("\n3) BEHAVIORAL VERIFICATION: simulated 5 consecutive 429 "
     "failures followed by successes, calling the real method against "
     "a fake client — not just reading the source")
fake_self, fake_client = make_fake_agent(fail_count=5)
method = agents.MarketDataAgent._poll_futures_via_rest
streak_history = []
for i in range(10):
    fake_self._last_quote_batch_call = 0
    fake_self._quote_batch_fail_until = 0
    method(fake_self)
    streak_history.append((getattr(fake_self, "_futures_429_streak", 0),
                          getattr(fake_self, "_futures_success_streak", 0)))

check("the 429 streak escalates 1->5 across the 5 simulated failures",
      [s[0] for s in streak_history[:5]] == [1, 2, 3, 4, 5],
      str(streak_history[:5]))
check("the 429 streak does NOT reset on the first success (call 6) — "
     "it stays at 5 while the success streak climbs",
      streak_history[5][0] == 5 and streak_history[5][1] == 1,
      str(streak_history[5]))
check("the 429 streak does NOT reset on the second success (call 7) "
     "either",
      streak_history[6][0] == 5 and streak_history[6][1] == 2,
      str(streak_history[6]))
check("the 429 streak FINALLY resets to 0 on the third consecutive "
     "success (call 8) — matching the intended 3-success recovery "
     "requirement exactly",
      streak_history[7][0] == 0 and streak_history[7][1] == 3,
      str(streak_history[7]))
check("stays recovered (0) on subsequent successes",
      streak_history[8][0] == 0 and streak_history[9][0] == 0)

print("\n4) BEHAVIORAL VERIFICATION: the actual pacing gap between two "
     "real, unmocked-time calls is ~4.0s, not the old 2.5s — measured "
     "directly with real elapsed time, not simulated")
fake_self2, fake_client2 = make_fake_agent(fail_count=0)
fake_client2.call_times = []
orig_quote_batch = fake_client2.quote_batch
def _tracking_quote_batch(seg_map):
    fake_client2.call_times.append(time.time())
    return orig_quote_batch(seg_map)
fake_client2.quote_batch = _tracking_quote_batch

start = time.time()
for _ in range(90):
    method(fake_self2)
    time.sleep(0.1)

check("at least 2 calls were observed in the 9-second window",
      len(fake_client2.call_times) >= 2, str(len(fake_client2.call_times)))
if len(fake_client2.call_times) >= 2:
    gap = fake_client2.call_times[1] - fake_client2.call_times[0]
    check("the measured gap between consecutive calls is close to 4.0s "
         "(3.9-4.2s, allowing for test-harness timing jitter)",
          3.9 <= gap <= 4.2, f"{gap:.2f}s")

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
