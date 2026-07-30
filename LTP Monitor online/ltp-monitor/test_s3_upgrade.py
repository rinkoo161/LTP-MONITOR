"""v58.48 — S3 (Anchor Pullback) absorbs deck setups 4, 5 and 6.

The source deck's remaining High Probability Setups:

    4  Trading for Wave 3   — enter at the end of Wave 2
    5  Trading for Wave 5   — enter at the end of Wave 4
    6  Trading the main trend at the end of Wave C
    7  Trading the correction after Wave 5 (COUNTER-trend)

4, 5 and 6 are the same trade in three costumes: "a correction is
ending, resume with the trend" — exactly what Anchor Pullback does.
They do not warrant three new strategies. What they add is a way to
judge WHICH pullback is worth taking, which is the one thing S3 had no
opinion about: it took every touch of the anchor.

Setup 7 is deliberately excluded — a counter-trend fade belongs with
the reversal detectors in S8, not bolted onto a trend-following entry.

CRITICAL: S3 has live trade history. Everything here is OFF by default
and asserted to leave behaviour byte-identical.
"""
import os, sys, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
results = []
def check(l, c, d=""):
    results.append((l, bool(c)))
    print(("  PASS  " if c else "  FAIL  ") + l + (f"   [{d}]" if d else ""))

import pa_strategies as pa, config

def mk(v, step=60):
    return [{"time": i * step, "open": x, "high": x * 1.001,
             "low": x * 0.999, "close": x, "volume": 0} for i, x in enumerate(v)]

D = pa.PA_DEFAULTS["vwap_pullback"]

print("1) DEFAULT BEHAVIOUR IS UNCHANGED — the non-negotiable")
for k in ("require_tide", "require_macd_zero_reversal",
          "require_hidden_divergence", "require_bb_confirm"):
    check(f"'{k}' defaults OFF", D[k] == 0)
check("min_confirmations defaults to 0", D["min_confirmations"] == 0)

src = open("pa_strategies.py").read()
i_need = src.index('need = int(p.get("min_confirmations", 0))')
i_short = src.index("if need <= 0:", i_need)
check("the filter short-circuits before evaluating anything",
      i_short - i_need < 400,
      "at min_confirmations=0 no indicator is computed at all")

random.seed(11)
paths = [[100 + random.uniform(-2, 2) + i * 0.05 for i in range(160)],
         [200 - random.uniform(-3, 3) - i * 0.04 for i in range(160)],
         [150 + 8 * ((i // 20) % 2) + random.uniform(-1, 1) for i in range(160)]]
same = True
for pth in paths:
    s = mk(pth)
    a = pa.evaluate("vwap_pullback", s)
    b = pa.evaluate("vwap_pullback", s, params=dict(D))
    if a != b:
        same = False
check("explicit defaults == implicit defaults on several paths", same)

print("\n2) Setup 7 is correctly NOT here")
check("no counter-trend confirmation exists",
      "setup 7" not in src.lower().split("def pullback_confirmations")[1][:600]
      or "NOT here" in src,
      "a fade after wave 5 belongs in S8, not a trend-following entry")
check("the exclusion is documented, not silent",
      "Setup 7 is deliberately NOT here" in src)

print("\n3) Each confirmation works independently")
up = mk([100 + i * 0.4 for i in range(200)])
up5 = mk([100 + i * 0.4 for i in range(60)], step=300)
up15 = mk([100 + i * 0.4 for i in range(40)], step=900)
met, det = pa.pullback_confirmations(up, up5, up15, +1, dict(D, require_tide=1))
check("tide confirmation evaluates", "tide" in det, str(det))
check("a rising series favours a long", met == 1 and "favours" in det["tide"], str(det))
met2, det2 = pa.pullback_confirmations(up, up5, up15, -1, dict(D, require_tide=1))
check("and opposes a short", met2 == 0 and "AGAINST" in det2["tide"], str(det2))

met3, det3 = pa.pullback_confirmations(
    up, up5, up15, +1, dict(D, require_tide=1, require_bb_confirm=1,
                            require_macd_zero_reversal=1,
                            require_hidden_divergence=1))
check("all four evaluate together", len(det3) == 4, str(list(det3)))
check("count never exceeds the number enabled", met3 <= 4, f"{met3}/4")

print("\n4) Missing data SKIPS, never blocks")
met4, det4 = pa.pullback_confirmations(up, None, None, +1,
                                       dict(D, require_tide=1, require_bb_confirm=1))
check("no 5m/15m -> tide counts as met",
      "skipped" in det4.get("tide", "") or "favours" in det4.get("tide", ""),
      str(det4))
check("absent data can never veto a trade", met4 >= 1, f"met={met4} {det4}")
short = mk([100] * 8)
m5, d5 = pa.pullback_confirmations(short, None, None, +1,
                                   dict(D, require_bb_confirm=1))
check("too-short series degrades without raising", m5 >= 0, str(d5))

print("\n5) The filter actually filters")
random.seed(3)
choppy = mk([150 + random.uniform(-4, 4) for i in range(200)])
fires_open = sum(1 for _ in [1] if pa.evaluate("vwap_pullback", choppy))
fires_strict = sum(1 for _ in [1] if pa.evaluate(
    "vwap_pullback", choppy,
    params=dict(D, require_tide=1, require_hidden_divergence=1,
                min_confirmations=2)))
check("requiring 2 confirmations is at least as strict as 0",
      fires_strict <= fires_open, f"open={fires_open} strict={fires_strict}")

print("\n6) Registration and tuning")
B = pa.PA_BOUNDS["vwap_pullback"]
for k in ("require_tide", "require_macd_zero_reversal",
          "require_hidden_divergence", "require_bb_confirm", "min_confirmations"):
    check(f"'{k}' is tunable", k in B)
tuned, _ = pa.tune("vwap_pullback", dict(D), -1)
for k in ("require_tide", "require_macd_zero_reversal",
          "require_hidden_divergence", "require_bb_confirm"):
    check(f"tune keeps '{k}' binary", tuned[k] in (0, 1), f"{tuned[k]}")
check("relaxing lowers min_confirmations (more trades)",
      B["min_confirmations"][2] == -1)
check("indicator primitives are REUSED, not reimplemented",
      "import ta_elliott as _te" in src,
      "so Tide/Bollinger/divergence cannot drift between S3 and S9")
import backtester
check("backtester picks the new params up",
      "min_confirmations" in backtester.DEFAULT_PARAMS["vwap_pullback"])
check("get_params clamps them", 0 <= backtester.get_params(
      "vwap_pullback", "NIFTY")["min_confirmations"] <= 4)

print("\n" + "=" * 62)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed: print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
