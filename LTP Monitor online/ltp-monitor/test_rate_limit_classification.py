"""v58.70 — a failure must not be called an expired token because a
price contained the digits 401.

`note_failure` classified with `"401" in text` and `"429" in text`,
bare substring tests against the whole error string. Real values that
trigger those: NIFTY spot 24015.75, security id 13401, quantity 4290.
The consequences are not cosmetic — an AUTH verdict takes a 30-minute
backoff (vs 30s for a transient error) and sets the `auth_expired`
panel state, so one timeout quoting the wrong price blanks every index
panel for half an hour and tells the operator to replace a token that
was never the problem.

Found on 2026-07-31 while checking a claim that the token was still
valid. It turned out not to be, that time -- but the classifier would
have said "expired" either way, which is precisely why it could not be
used as evidence.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import rate_limit as rl


def classify(text_or_exc):
    """(is_auth, is_429, backoff_seconds) for one failure, in isolation."""
    rl.reset("probe")
    is_429, secs = rl.note_failure(text_or_exc, resource="probe")
    return rl.is_auth_failure("probe"), is_429, secs


print("1) the false positives that motivated this")
for text, label in (
        ("Timeout fetching NIFTY at spot 24015.75", "price containing 401"),
        ("HTTP 500 from Dhan for securityId 13401", "security id containing 401"),
        ("Dhan error: quantity 4012 rejected", "quantity containing 401"),
        ("connection reset while quoting 4290 lots", "quantity containing 429")):
    auth, is429, secs = classify(text)
    check(f"{label} is an ordinary error", not auth and not is429,
          f"auth={auth} 429={is429} backoff={secs}s")
    check(f"  ...and gets the SHORT backoff, not 30 minutes", secs == rl.BACKOFF_OTHER,
          f"{secs}s")

print("\n2) genuine auth failures still classify (regression)")
for text in ("401 Unauthorized",
             "401 Client Error: Unauthorized for url: https://api.dhan.co/v2",
             "Dhan token expired -- paste a fresh Access Token",
             "Dhan says 401 Unauthorized - your access token has expired",
             '{"errorType":"Invalid_Authentication","errorCode":"DH-901"}'):
    auth, _, secs = classify(text)
    check(f"auth: {text[:46]}", auth and secs == rl.BACKOFF_AUTH, f"{secs}s")

print("\n3) genuine rate limits still classify (regression)")
for text in ("429 Too Many Requests", "Dhan rate limit hit: HTTP 429"):
    auth, is429, secs = classify(text)
    check(f"429: {text[:46]}", is429 and not auth and secs == rl.BACKOFF_429,
          f"{secs}s")

print("\n4) a real HTTP status beats the message text")
class FakeResp:
    def __init__(self, code): self.status_code = code
class FakeErr(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.response = FakeResp(code)

auth, is429, secs = classify(FakeErr("server blew up quoting 24015.75", 500))
check("a 500 whose text contains 401 is NOT auth", not auth and not is429,
      f"auth={auth} backoff={secs}s")
auth, is429, _ = classify(FakeErr("something went wrong", 401))
check("a real 401 is auth even with an unhelpful message", auth)
auth, is429, _ = classify(FakeErr("slow down", 429))
check("a real 429 is a rate limit", is429 and not auth)

print("\n5) the panel state that depends on it")
rl.reset("quote")
rl.note_failure("Timeout fetching NIFTY at spot 24015.75", "quote")
check("a timeout does NOT put the UI into auth_expired",
      not rl.is_auth_failure("quote"), str(rl.why("quote")))
rl.reset("quote")
rl.note_failure("401 Unauthorized", "quote")
check("a real 401 still does", rl.is_auth_failure("quote"))
rl.reset("quote")

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS -- all {len(results)} checks")
