"""v58.20+ — tests for quieting the repeated "entry failed" log spam.

Root cause, confirmed directly from a live log: a spread's entry-
failure reason logged unconditionally every single cycle, even when
the reason was identical to the previous cycle — 596 near-identical
lines in one log file, 595 of them "already open on X" (a persistent
condition that doesn't change while the existing position stays open).
Doesn't cost money, just drowns out genuinely new information.

Fix: _should_log_entry_fail() only allows a log when the reason for
that symbol/strategy pair has genuinely changed, or when at least 10
minutes have passed since the last time that exact reason was logged
— otherwise stays silent.

Run:  python3 test_entry_fail_log_throttle.py
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

print("1) source-level: the throttling helper exists and the "
     "unconditional log call it replaced is genuinely gone")
check("_should_log_entry_fail method is defined",
      "def _should_log_entry_fail(self, fail_key, reason):" in src)
check("the entry-failed log call is now gated behind it",
      "if self._should_log_entry_fail(f\"{sym}:{name}\", reason):" in src)

print("\n2) BEHAVIORAL VERIFICATION: repeating the identical reason for "
     "the same symbol/strategy pair only logs once, matching the "
     "exact live pattern (595 identical 'already open' lines that "
     "should have been 1)")
fake_self = types.SimpleNamespace()
method = agents.ExecutionAgent._should_log_entry_fail

# The 10 identical-reason calls in a row should log exactly once (the first)
log_decisions = [method(fake_self, "SENSEX:bear_call_spread",
                        "bear_call_spread already open on SENSEX at 77000")
                 for _ in range(10)]
check("first call logs (returns True)", log_decisions[0] is True)
check("all 9 subsequent identical-reason calls stay silent (return "
     "False) — this is the exact fix for the 595-line spam",
      all(d is False for d in log_decisions[1:]),
      str(log_decisions))

print("\n3) BEHAVIORAL VERIFICATION: a genuinely different reason for "
     "the SAME symbol/strategy pair logs immediately, not silently")
changed = method(fake_self, "SENSEX:bear_call_spread",
                 "R1 wall 26100 is 99 pts above spot; credit too thin")
check("a changed reason logs immediately (returns True)", changed is True)

print("\n4) BEHAVIORAL VERIFICATION: reverting back to the ORIGINAL "
     "reason after a change also logs immediately (the baseline "
     "correctly tracks the CURRENT reason, not just 'has this reason "
     "ever been seen before')")
reverted = method(fake_self, "SENSEX:bear_call_spread",
                  "bear_call_spread already open on SENSEX at 77000")
check("reverting to the prior reason logs again (returns True)",
      reverted is True)

print("\n5) BEHAVIORAL VERIFICATION: a different symbol/strategy pair "
     "is tracked independently — one pair's silence doesn't suppress "
     "another pair's genuinely new failure")
different_pair = method(fake_self, "NIFTY:bull_put_spread",
                        "S1 wall 23950 is 14 pts below spot")
check("a different fail_key logs independently, unaffected by the "
     "SENSEX pair's throttling state",
      different_pair is True)

print("\n6) BEHAVIORAL VERIFICATION: after 10+ minutes (600s) with the "
     "SAME reason, a periodic heartbeat log fires again rather than "
     "staying silent forever — confirms the fix doesn't go completely "
     "quiet on a persistent condition, just stops spamming every cycle")
# Manually age the recorded timestamp past the 600s threshold
fake_self._entry_fail_last["SENSEX:bear_call_spread"] = (
    "bear_call_spread already open on SENSEX at 77000", time.time() - 601)
heartbeat = method(fake_self, "SENSEX:bear_call_spread",
                   "bear_call_spread already open on SENSEX at 77000")
check("a heartbeat log fires after 601s with the same reason",
      heartbeat is True)

# And confirm it goes quiet again immediately after that heartbeat
quiet_again = method(fake_self, "SENSEX:bear_call_spread",
                     "bear_call_spread already open on SENSEX at 77000")
check("goes silent again immediately after the heartbeat fires",
      quiet_again is False)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
