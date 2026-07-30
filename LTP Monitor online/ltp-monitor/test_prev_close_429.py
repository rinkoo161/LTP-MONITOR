"""v58.34 — regression guard for two bugs found in a live log at
2026-07-29 00:51.

The log showed, in order:

    prev_close_for — batch quote_batch() for all 4 symbols raised 429
    NIFTY:     prev_close_for — quote_batch() raised 429 — falling back
    BANKNIFTY: prev_close_for — quote_batch() raised 429 — falling back
    FINNIFTY:  prev_close_for — quote_batch() raised 429 — falling back
    SENSEX:    prev_close_for — quote_batch() raised 429 — falling back

FIVE requests where ONE had already been refused.

BUG 1 — 429 amplification. When the shared batch call failed, every
symbol fell through to its own per-symbol quote_batch(). A 429 does not
mean "that batch was malformed, try them individually"; it means stop.
The per-symbol path is a real safety net for a batch that returned
missing a symbol, but it is the worst possible response to a rate
limit, and it triggers exactly when the endpoint is already contended.

BUG 2 — a transient 429 disabled the feature for the whole day. The
"already tried today" flag was set BEFORE the call, so one failure at
00:51 (outside market hours, when nobody would notice) meant every
symbol used candle reconstruction until midnight — reintroducing the
small displayed-change drift the authoritative quote path exists to
remove. The flag is now set only on success.

Run:  python3 test_prev_close_429.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


import app  # noqa: E402


class Boom429:
    """A quote endpoint that always 429s, counting every call."""
    calls = 0

    def quote_batch(self, payload):
        Boom429.calls += 1
        raise Exception("429 Client Error: Too Many Requests for url: "
                        "https://api.dhan.co/v2/marketfeed/quote")


class Flaky:
    """429s once, then succeeds — the transient case."""
    calls = 0

    def quote_batch(self, payload):
        Flaky.calls += 1
        if Flaky.calls == 1:
            raise Exception("429 Client Error: Too Many Requests")
        ids = payload.get("IDX_I", [])
        return {"IDX_I": {str(i): {"previous_close_price": 100.0 + n,
                                   "last_price": 250.0 + n}
                          for n, i in enumerate(ids)}}


def reset(day="2026-07-29"):
    app._prev_close_batch_tried = None
    app._reset_quote_rate_limit()   # v58.49: also clears the SHARED registry
    app._prev_close = {}
    return day


print("1) A 429 must NOT be amplified into per-symbol retries")
_orig_fb, _orig_dc = app._dhan_fallback_client, app.dhan_client
day = reset()
Boom429.calls = 0
app._dhan_fallback_client = lambda: Boom429()
app.dhan_client = lambda: Boom429()

app._batch_fetch_prev_close(day)
after_batch = Boom429.calls
check("the shared batch makes exactly ONE call", after_batch == 1,
      f"{after_batch} calls")
check("a 429 sets the rate-limit cooldown", app._quote_rate_limited() is True,
      f"until={app._quote_rate_limited_until:.0f}")

# Now every symbol asks for its prev close, as the live log showed.
for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
    try:
        app.prev_close_for(sym)
    except Exception:
        pass
check("four symbols add ZERO further quote calls while rate-limited",
      Boom429.calls == after_batch,
      f"{Boom429.calls} total (was {after_batch}); the live bug made it 5")

print("\n2) A transient 429 must not disable the feature for the whole day")
day = reset()
Flaky.calls = 0
app._dhan_fallback_client = lambda: Flaky()
app.dhan_client = lambda: Flaky()

app._batch_fetch_prev_close(day)
check("first attempt fails", Flaky.calls == 1)
check("the day is NOT marked done after a failure",
      app._prev_close_batch_tried != day,
      f"flag={app._prev_close_batch_tried!r}")

# Cooldown blocks an immediate retry...
app._batch_fetch_prev_close(day)
check("cooldown suppresses an immediate retry", Flaky.calls == 1,
      f"{Flaky.calls} calls")

# ...but once it lapses the batch retries and succeeds.
app._reset_quote_rate_limit()
app._batch_fetch_prev_close(day)
check("batch retries after the cooldown lapses", Flaky.calls == 2)
check("the day IS marked done after success",
      app._prev_close_batch_tried == day, f"flag={app._prev_close_batch_tried!r}")
check("prev_close values were actually populated",
      len(app._prev_close) > 0, f"{len(app._prev_close)} symbols cached")

app._batch_fetch_prev_close(day)
check("a successful day is not re-fetched", Flaky.calls == 2)

print("\n3) Non-429 failures get a shorter cooldown than a rate limit")
reset()
app._note_quote_failure(Exception("Connection reset by peer"))
short = app._quote_rate_limited_until - time.time()
reset()
app._note_quote_failure(Exception("429 Too Many Requests"))
long_ = app._quote_rate_limited_until - time.time()
check("429 backs off longer than a transient network error", long_ > short,
      f"429={long_:.0f}s vs other={short:.0f}s")
check("a transient error still backs off at least briefly", short > 0)

app._dhan_fallback_client, app.dhan_client = _orig_fb, _orig_dc
reset()

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
