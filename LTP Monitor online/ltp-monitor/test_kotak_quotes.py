#!/usr/bin/env python3
"""test_kotak_quotes.py — v59.96.

Kotak's REST quotes path, re-implemented rather than taken from the
vendor SDK because `kotakneoapi` pins `pandas<3` while this environment
runs pandas 3.0.5 — installing it would DOWNGRADE pandas underneath
dhanhq (the live feed) and yfinance (the primary macro provider).

These checks are mostly about the four places a setting has to exist
before it works, because this codebase has a documented history of a
config key that looked wired up and silently vanished:

    config.DEFAULTS   or config.save() drops it on the first save
    SettingsIn        or pydantic discards it one layer earlier
    the save collector in dashboard.html
    the load/populate path in dashboard.html

A field present in three of the four is the failure mode that is
hardest to spot, because the UI looks right.

No network. The live probe belongs in the Settings page's button, not
in a test that must pass without credentials.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_kotak_quotes")

import config
import kotak_quotes

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


H = open("static/dashboard.html").read()
APP = open("app.py").read()

print("1) the two keys exist in all FOUR layers")
for k in ("kotak_consumer_key", "kotak_base_url"):
    check(f"{k}: in config.DEFAULTS", k in config.DEFAULTS,
          "config.save() silently drops anything not in DEFAULTS")
    check(f"{k}: declared on SettingsIn",
          re.search(rf"\n\s+{k}: str \| None = None", APP) is not None,
          "pydantic drops undeclared fields before config.save() ever sees them")
    check(f"{k}: sent by the save collector",
          re.search(rf"{k}:document\.getElementById", H) is not None)
check("consumer key is populated back into the form",
      's_kck_state' in H and 'kotak_consumer_key_set' in H)
check("base url is populated back into the form",
      re.search(r'getElementById\("s_kbase"\)\.value=settings\.kotak_base_url', H)
      is not None)

print("\n2) the consumer key is treated as a secret, the base URL is not")
check("kotak_consumer_key is in SECRET_KEYS",
      "kotak_consumer_key" in config.SECRET_KEYS,
      "otherwise the raw key is shipped to the browser by public_view()")
check("kotak_base_url is NOT a secret", "kotak_base_url" not in config.SECRET_KEYS,
      "it must arrive unmasked or the form cannot show the current value")
check("the form never pre-fills the secret input",
      'getElementById("s_kck").value=""' in H,
      "pre-filling a mask string means a stray save overwrites a good key "
      "with literal dots")

print("\n3) the probe endpoint exists and is read-only")
check("POST /api/kotak/quotes_probe is registered",
      '@app.post("/api/kotak/quotes_probe")' in APP)
_i = APP.index('@app.post("/api/kotak/quotes_probe")')
BODY = APP[_i:APP.index("\n@app.", _i + 10)]
check("it writes no config", "config.save(" not in BODY,
      "a credential TEST must not persist anything")
check("it places no order", "manual_trade" not in BODY and "enter_" not in BODY)
check("the button saves before probing",
      re.search(r"await saveSettings\(\);\s*\n\s*const d=await \(await "
                r'fetch\("/api/kotak/quotes_probe"', H) is not None,
      "probe() reads the STORED key, so testing an unsaved value would "
      "report on the previous one")

print("\n4) the client shares the process-wide cooldown registry")
SRC = open("kotak_quotes.py").read()
check("it checks rate_limit before calling", "rate_limit.is_limited(" in SRC)
check("it reports failures into rate_limit", "rate_limit.note_failure(" in SRC)
check("one coarse resource name, not a private cooldown",
      'RESOURCE = "kotak_quote"' in SRC and SRC.count("RESOURCE") >= 4,
      "independent cooldowns on one broker's endpoints was a real bug")

print("\n5) it refuses bad input clearly, without a network call")
cfg = {"kotak_base_url": "", "kotak_consumer_key": "x" * 20}
try:
    kotak_quotes.quotes([("nse_cm", "26000")], cfg=cfg)
    check("missing base_url raises", False, "no exception")
except kotak_quotes.KotakQuotesError as e:
    check("missing base_url raises a named error", "kotak_base_url" in str(e), str(e)[:70])
except Exception as e:
    check("missing base_url raises the RIGHT error", False, repr(e))

cfg = {"kotak_base_url": "https://example.invalid", "kotak_consumer_key": "W1DZT"}
try:
    kotak_quotes.quotes([("nse_cm", "26000")], cfg=cfg)
    check("a UCC-shaped key is rejected before the network", False, "no exception")
except kotak_quotes.KotakQuotesError as e:
    check("a UCC-shaped key is rejected before the network, and says why",
          "UCC" in str(e), str(e)[:110])
except Exception as e:
    check("a UCC-shaped key raises the RIGHT error", False, repr(e))

try:
    kotak_quotes.quotes([("nse_cm", "26000")], quote_type="nope",
                        cfg={"kotak_base_url": "x", "kotak_consumer_key": "y" * 20})
    check("an unknown quote_type is rejected", False, "no exception")
except ValueError:
    check("an unknown quote_type is rejected", True)
except Exception as e:
    check("an unknown quote_type raises ValueError", False, repr(e))

check("empty token list short-circuits without a call",
      kotak_quotes.quotes([], cfg={"kotak_base_url": "x",
                                   "kotak_consumer_key": "y" * 20}) == [])

print("\n5b) a `fault` payload is a FAILURE, not a success")
# Found live 2026-08-17 on the first real call. Kotak answers an invalid
# instrument token with HTTP 200 and a `fault` object rather than the
# `error` key the code originally looked for, so probe() reported
# ok=True on a rejected request. A checker that reports success while
# the request failed is worse than no checker.
check("the fault key is handled, not just error", '"fault"' in SRC or "'fault'" in SRC)


class _FaultResp:
    status_code = 200
    text = '{"fault": {"code": "400", "description": "Invalid neosymbol values"}}'

    @staticmethod
    def json():
        return {"fault": {"code": "400",
                          "description": "Please pass valid neosymbol values"}}


_real_get = kotak_quotes.requests.get
kotak_quotes.requests.get = lambda *a, **k: _FaultResp()
try:
    kotak_quotes.quotes([("nse_cm", "1")],
                        cfg={"kotak_base_url": "https://x",
                             "kotak_consumer_key": "k" * 20})
    check("a fault response raises", False, "returned normally")
except kotak_quotes.KotakQuotesError as e:
    check("a fault response raises, quoting the description",
          "neosymbol" in str(e), str(e)[:90])
except Exception as e:
    check("a fault response raises KotakQuotesError", False, repr(e))
finally:
    kotak_quotes.requests.get = _real_get

check("probe() uses a documented, verified instrument token",
      '"1333"' in SRC,
      "the first version guessed an index token that does not resolve, so "
      "a WORKING key reported as broken")

print("\n6) 'oi' is available — the chain needs it, not just price")
check("oi is a supported quote type", "oi" in kotak_quotes.QUOTE_TYPES,
      "analyzer.classify_leg() is driven by per-strike OI and its change")

print("\n7) nothing here is wired into a trading path yet")
for mod in ("agents.py", "analyzer.py", "strategies.py", "backtester.py"):
    check(f"{mod} does not import kotak_quotes",
          "kotak_quotes" not in open(mod).read(),
          "validate against the real API before anything depends on it")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: " + ", ".join(FAILED))
    sys.exit(1)
print("all kotak-quotes checks passed")
