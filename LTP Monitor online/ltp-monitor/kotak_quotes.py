"""kotak_quotes.py — Kotak Neo REST quotes, without the vendor SDK.

WHY THIS EXISTS RATHER THAN `pip install kotakneoapi`
-----------------------------------------------------
Kotak's official SDK is MIT-licensed and well documented, and its
`quotes()` is exactly what this system wants: batched, ~289 ms round
trip (their figure; 250-350 ms typical), and — unusually — needing NO
2FA session, only a consumer key. Against Dhan's option-chain limit of
one request per three seconds, that is roughly a 10x improvement in how
fresh the chain can be.

It is not installed because `kotakneoapi` pins `pandas<3` while this
environment runs pandas 3.0.5, so installing it would DOWNGRADE pandas
underneath `dhanhq` (the live market-data websocket) and `yfinance`
(the primary macro provider, deliberately version-pinned because a
silent break there matters). Trading two live data paths for one
dependency is a bad exchange.

The REST contract is fully published, so this re-implements it against
`requests` — already a dependency — which is CLAUDE.md rule 4's "adopt
techniques, re-implement" rather than vendoring. Roughly forty lines
against ten new packages and a pandas downgrade.

The SDK still has a real advantage for the STREAMING feed (SFeed), and
that is deliberately not attempted here: the existing `kotak_ws.py`
protocol is reverse-engineered from Kotak's JS library and its own
docstring records that it has never been confirmed against a live
connection. If streaming is wanted, the right shape is the official SDK
in a SEPARATE PROCESS with its own virtualenv, publishing ticks into the
bus — which is the pattern already used to keep Kite out of this
process — not a pandas downgrade here.

ENDPOINT (documented)
    GET {base}/script-details/1.0/quotes/neosymbol/{seg}|{tok},.../{type}
    header: Authorization: <consumer_key>
    type: all | market_depth | ohlc | ltp | oi | 52w | circuit_limits
          | scrip_details

`oi` matters: analyzer.classify_leg() is driven by per-strike open
interest and its change, so a quote type that returns OI is the
difference between this being useful for the chain and being a price
ticker.

NOT WIRED INTO ANY TRADING PATH. Nothing here is called by an agent,
the analyzer or a strategy; `broker` stays on whatever Settings says.
This module exists so the path can be validated against the real API
before anything depends on it — the same discipline kotak_ws.py's
docstring asks for and which, being dead code, it never received.
"""
import time

import requests

import config
import rate_limit

# Every Kotak REST call shares the one process-wide cooldown registry
# under this coarse resource name. Independent per-endpoint cooldowns on
# one broker was a real bug once; do not add a private one here.
RESOURCE = "kotak_quote"

QUOTE_TYPES = ("all", "market_depth", "ohlc", "ltp", "oi", "52w",
               "circuit_limits", "scrip_details")
DEFAULT_TIMEOUT = 10


class KotakQuotesError(RuntimeError):
    pass


def _base_url(cfg):
    base = (cfg.get("kotak_base_url") or "").strip().rstrip("/")
    if not base:
        raise KotakQuotesError(
            "kotak_base_url is not set — paste the Base URL from the Kotak "
            "Neo API portal in Settings")
    return base


def _consumer_key(cfg):
    ck = (cfg.get("kotak_consumer_key") or "").strip()
    if not ck:
        raise KotakQuotesError(
            "kotak_consumer_key is not set — paste the Consumer Key from the "
            "Kotak Neo API portal in Settings")
    # 2026-08-16: the stored value was "W1DZT", which is the UCC — it is
    # also the UCC field's own placeholder text, so it is an easy paste
    # error to make and the API's only complaint is a flat "Consumer key
    # is invalid". Say something more useful than the server does.
    if len(ck) < 12:
        raise KotakQuotesError(
            f"kotak_consumer_key looks wrong ({len(ck)} chars). A Kotak "
            f"consumer key is a long token; a short value here is usually "
            f"the UCC pasted into the wrong field.")
    return ck


def quotes(tokens, quote_type="all", cfg=None, timeout=DEFAULT_TIMEOUT):
    """Batched quotes for [(segment, token), ...].

    segment is Kotak's exchange segment string, e.g. "nse_cm" for cash
    or "nse_fo" for F&O. Returns the decoded JSON list.

    Batched deliberately: the whole point over Dhan is fetching a full
    chain in ONE call, so callers should pass every strike at once
    rather than looping.
    """
    if quote_type not in QUOTE_TYPES:
        raise ValueError(f"quote_type must be one of {QUOTE_TYPES}")
    pairs = [(str(s), str(t)) for s, t in tokens]
    if not pairs:
        return []
    cfg = cfg or config.load()

    if rate_limit.is_limited(RESOURCE):
        raise KotakQuotesError(
            f"kotak quotes cooling down: {rate_limit.why(RESOURCE)}")

    url = (f"{_base_url(cfg)}/script-details/1.0/quotes/neosymbol/"
           + ",".join(f"{s}|{t}" for s, t in pairs)
           + f"/{quote_type}")
    headers = {"Authorization": _consumer_key(cfg),
               "Accept": "application/json"}
    t0 = time.time()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
    except Exception as e:
        rate_limit.note_failure(e, resource=RESOURCE)
        raise KotakQuotesError(f"kotak quotes request failed: {e}") from e

    elapsed_ms = (time.time() - t0) * 1000
    if r.status_code != 200:
        # Let the shared registry decide the cooldown: it already knows
        # 429 means back off hard and an auth failure means back off
        # much harder, and it applies that uniformly across brokers.
        rate_limit.note_failure(
            f"HTTP {r.status_code}: {r.text[:160]}", resource=RESOURCE)
        raise KotakQuotesError(
            f"kotak quotes HTTP {r.status_code} in {elapsed_ms:.0f}ms: "
            f"{r.text[:200]}")
    try:
        data = r.json()
    except Exception as e:
        raise KotakQuotesError(
            f"kotak quotes returned non-JSON: {r.text[:200]}") from e
    # Kotak signals a bad request TWO ways, and only one of them is the
    # obvious `error` key. An invalid instrument token comes back as
    # HTTP 200 with a `fault` object:
    #
    #   {"fault": {"code": "400", "description": "Please pass valid
    #              neosymbol values for getQuote", ...}}
    #
    # Found 2026-08-17 on the very first live call: probe() reported
    # ok=True on exactly that payload, because it only looked for
    # `error` and a 200 status. A checker that reports success while the
    # request failed is the failure mode this project keeps finding
    # elsewhere; it is not acceptable in the thing whose only job is
    # checking.
    if isinstance(data, dict):
        fault = data.get("fault")
        if fault:
            desc = (fault.get("description") or fault.get("message")
                    or str(fault))
            raise KotakQuotesError(f"kotak quotes rejected the request: {desc}")
        if data.get("error"):
            raise KotakQuotesError(f"kotak quotes error: {data.get('message')}")
    return data


def probe(cfg=None):
    """One-shot reachability + credential check. Returns a dict, never raises.

    Used by the Settings page's "Test Kotak quotes" button so a bad
    consumer key is reported where it is pasted, rather than surfacing
    later as an empty chain.
    """
    cfg = cfg or config.load()
    out = {"ok": False, "latency_ms": None, "error": None, "sample": None}
    t0 = time.time()
    try:
        # HDFCBANK on the cash segment — the token from Kotak's own
        # documented example, verified live 2026-08-17 (277 ms, ltp 727).
        #
        # Deliberately NOT an index. The first version probed
        # nse_cm|26000 for NIFTY, guessing that Kotak uses the same index
        # token as other brokers; it does not, and the call failed. The
        # probe's job is to answer "is this credential good?", so it must
        # use an instrument that is certain to resolve — otherwise a
        # working key reads as broken and the operator re-pastes a
        # perfectly good credential. Index and F&O token resolution
        # belongs with the scrip master, not here.
        data = quotes([("nse_cm", "1333")], quote_type="ltp", cfg=cfg)
        if not data:
            raise KotakQuotesError("empty response")
        out["ok"] = True
        out["sample"] = data[:1] if isinstance(data, list) else data
    except Exception as e:
        out["error"] = str(e)
    out["latency_ms"] = round((time.time() - t0) * 1000)
    return out
