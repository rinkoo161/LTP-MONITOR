"""news_engine.py — shared news fetching, categorization, and impact
scoring, used by BOTH NewsAgent (bus key "news", the live risk-gating
signal) and NewsMacroAgent (the structured Macro/News event log).

WHY THIS EXISTS (2026-07-24): NewsAgent and NewsMacroAgent used to
fetch and classify news completely independently — NewsAgent pulled
one Google-News RSS query, NewsMacroAgent made its own separate
NewsAPI.org calls for global_macro/constituent/weather categories. In
practice both ended up processing the SAME underlying Indian-market
stories (e.g. "Sensex falls 700 points") through two separate, slower,
differently-classified pipelines — exactly the "picking similar
information again and again" the user flagged. This module is the fix:
ONE fetch-and-classify pipeline, shared by both, with a bus-backed
de-dup set so the same headline is never processed twice.

NewsAgent's specific contract (bus key "news" with risk_event/
sentiment/flagged_ts, consumed by news_risk_opportunity() in the live
risk-gating pipeline) is UNCHANGED — this module feeds it richer,
de-duplicated input, it does not change what that gate does or how it
behaves. That gate is close to live trading and deliberately was not
touched beyond swapping its data source.

RSS over NewsAPI: NewsAPI.org has real per-day call limits (already a
constraint on NewsMacroAgent's design); RSS feeds are free, no key
needed, and update continuously. This module fetches RSS as the
primary source; NewsAPI (where configured) remains available as a
supplementary source for categories RSS doesn't cover well.

HONEST STATUS: this sandbox's egress allowlist blocks every news
domain tested (moneycontrol.com, economictimes.com, cnbc.com — same
403 host_not_allowed restriction hit with images.dhan.co / Dhan's
websocket earlier). None of the feed URLs below have been fetched
live from this environment. The Indian feeds are the user's own
confirmed-working sources; the global feeds are widely-documented,
long-established RSS endpoints (CNBC's ID-based RSS pattern, Yahoo
Finance) cited consistently across independent RSS directories — but
"widely documented" isn't "verified by me right now". Use the
test-feed function/endpoint (test_feed() below, exposed via
/api/news/feeds/test) to validate each source from a machine that CAN
reach these domains before relying on any of them.
"""
import json
import os
import store
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STORE_DIR = store.home()
FEEDS_FILE = os.path.join(STORE_DIR, "news_feeds.json")


def now_ist():
    return datetime.now(IST)


# ---------------------------------------------------------------- feeds

DEFAULT_FEEDS = [
    # User-confirmed working Indian sources
    {"id": "moneycontrol", "name": "Moneycontrol Latest News",
     "url": "http://www.moneycontrol.com/rss/latestnews.xml",
     "category": "market", "region": "india", "enabled": True},
    {"id": "economic_times", "name": "Economic Times",
     "url": "https://economictimes.indiatimes.com/rssfeedsdefault.cms",
     "category": "business", "region": "india", "enabled": True},
    {"id": "financial_express", "name": "The Financial Express",
     "url": "https://www.financialexpress.com/feed/",
     "category": "business", "region": "india", "enabled": True},
    {"id": "business_line", "name": "Business Line - Home",
     "url": "https://www.thehindubusinessline.com/feeder/default.rss",
     "category": "business", "region": "india", "enabled": True},
    # Global — widely-documented long-standing endpoints, NOT live-
    # tested from this sandbox (see module docstring). Run test_feed()
    # on each before trusting it.
    {"id": "cnbc_world", "name": "CNBC World Top News",
     "url": "https://www.cnbc.com/id/100727362/device/rss/rss.html",
     "category": "global-macro", "region": "global", "enabled": True},
    {"id": "cnbc_economy", "name": "CNBC Economy",
     "url": "https://www.cnbc.com/id/20910258/device/rss/rss.html",
     "category": "economics", "region": "global", "enabled": True},
    {"id": "cnbc_finance", "name": "CNBC Finance",
     "url": "https://www.cnbc.com/id/10000664/device/rss/rss.html",
     "category": "market", "region": "global", "enabled": True},
    {"id": "cnbc_energy", "name": "CNBC Energy",
     "url": "https://www.cnbc.com/id/19836768/device/rss/rss.html",
     "category": "energy", "region": "global", "enabled": True},
    {"id": "yahoo_finance", "name": "Yahoo Finance News",
     "url": "https://finance.yahoo.com/news/rssindex",
     "category": "market", "region": "global", "enabled": True},
]

# Categories this module classifies into. "other" is the catch-all —
# every category below has a keyword set; if nothing matches, "other".
CATEGORY_KEYWORDS = {
    "geopolitical": ("war", "conflict", "sanction", "sanctions", "military",
                     "missile", "invasion", "ceasefire", "geopolit",
                     "diplomat", "election", "coup"),
    "economics": ("gdp", "inflation", "cpi", "wpi", "interest rate",
                  "repo rate", "rbi", "federal reserve", "fed ", "budget",
                  "fiscal", "deficit", "recession", "unemployment"),
    "energy": ("crude", "oil price", "opec", "natural gas", "energy",
              "renewable", "solar", "power plant", "coal"),
    "banking": ("bank", "npa", "loan", "deposit", "psu bank", "private bank",
               "banking sector", "credit growth"),
    "mergers": ("merger", "acquisition", "acquire", "stake sale", "takeover",
               "buyout", "m&a"),
    "auto": ("auto sector", "automobile", "ev sales", "electric vehicle",
            "car sales", "two-wheeler", "vehicle sales", "auto stocks"),
    "tech": ("semiconductor", "artificial intelligence", " ai ", "software",
            "it sector", "chip", "data center", "cloud computing"),
    "weather": ("monsoon", "cyclone", "rainfall", "drought", "flood"),
    "market": ("sensex", "nifty", "bse", "nse", "stock market", "index",
              "ipo", "listing", "fii", "dii"),
}
CATEGORY_RE = {
    cat: re.compile(r"\b(" + "|".join(re.escape(w) for w in kws) + r")\b", re.I)
    for cat, kws in CATEGORY_KEYWORDS.items()
}

BEARISH_WORDS_RE = re.compile(
    r"\b(decline\w*|fall\w*|fell|drop\w*|crash\w*|plunge\w*|slump\w*|"
    r"tumble\w*|weak\w*|cut|cuts|lower\w*|downgrade\w*|"
    r"tension\w*|sanction\w*|tariff\w*|war|conflict\w*|"
    r"deficit\w*|slowdown\w*|recession\w*|selloff|sell-off)\b", re.I)
BULLISH_WORDS_RE = re.compile(
    r"\b(rally|rallie\w*|surge\w*|gain\w*|ris\w*|rose|growth|grew|strong\w*|"
    r"strengthen\w*|upgrade\w*|record high|all-time high|beat estimates|"
    r"beats estimates|outperform\w*|rebound\w*|recover\w*)\b", re.I)

# 2026-07-27 — real gap found: `valid` only checked non-empty title,
# with NO actual relevance check at all — an RSS item about sports,
# entertainment, or unrelated world news that happened to contain a
# generic bearish/bullish word (e.g. "fall" in a weather report, "war"
# in an unrelated headline) would be classified with a real bias and,
# via HIGH_SEVERITY_RE below, could get the MOST aggressive market-
# impact read even with zero actual financial content. CATEGORY_RE
# above is narrower (specific keyword sets per category); this is a
# broader net specifically for "is this headline about markets/finance
# AT ALL," used as an additional relevance gate before any bias/
# severity classification is trusted, not a replacement for CATEGORY_RE.
FINANCIAL_CONTEXT_RE = re.compile(
    r"\b(market\w*|stock\w*|share\w*|index|indices|trading|invest\w*|"
    r"rupee\w*|dollar\w*|currency|econom\w*|financial|exchange\w*|"
    r"corporate|compan\w*|firm\w*|earning\w*|revenue|profit\w*|"
    r"quarter\w*|equit\w*|bond\w*|yield\w*)\b", re.I)

# Impact-window heuristic (2026-07-24): a category+severity based
# estimate of how long a headline is likely to matter to a trading
# decision, NOT a validated/backtested prediction model. HIGH-severity
# keyword hits (rate decisions, war, crash-level moves) are the kind
# of headline that typically causes both an immediate spike AND a
# sustained short-term move, so all three windows are flagged. MEDIUM
# severity (routine sector/constituent news) usually takes a moment to
# be digested and priced in -- 5m/15m, not an instant 1m spike.
# LOW/routine items get no window at all. This is a starting point
# meant to be refined against real outcomes, same discipline used for
# the spread profit-targets and the ATR-stop clamp earlier in this
# project -- not a finished prediction model.
HIGH_SEVERITY_RE = re.compile(
    r"\b(rate decision|repo rate|rbi policy|fed decision|war|invasion|"
    r"crash\w*|plunge\w*|surge\w*|circuit breaker|halt trading|"
    r"emergency|sanction\w*|default\w*|bankrupt\w*)\b", re.I)


def classify_bias(title):
    has_bear = bool(BEARISH_WORDS_RE.search(title))
    has_bull = bool(BULLISH_WORDS_RE.search(title))
    if has_bear and not has_bull:
        return "bearish"
    if has_bull and not has_bear:
        return "bullish"
    return "neutral"


def classify_category(title):
    for cat, rx in CATEGORY_RE.items():
        if rx.search(title):
            return cat
    return "other"


def classify_impact_window(title, category, is_relevant=None):
    """Returns a list of windows this headline is estimated to affect:
    subset of ["1m", "5m", "15m"], or [] if not expected to move price
    at candle level at all.

    2026-07-27 — real bug found: HIGH_SEVERITY_RE used to run
    UNCONDITIONALLY, before any category/relevance check at all. A
    totally unrelated headline (sports, entertainment, a car accident)
    containing any of its generic severity words ("crash", "war",
    "emergency") got the MOST aggressive 3-window classification
    regardless of whether the story had any financial content
    whatsoever. Now requires the headline to be relevant at all
    (a real category, or FINANCIAL_CONTEXT_RE) before HIGH_SEVERITY_RE
    is trusted — an irrelevant headline gets [] regardless of which
    severity words it happens to contain.

    `is_relevant` accepts an EXPLICIT override — added after finding
    this function would otherwise re-derive relevance independently
    via keywords even when the caller already has a MORE authoritative
    signal (the AI classifier's own relevant judgment). Re-deriving it
    here separately could disagree with whatever actually governed the
    bias determination upstream, producing the confusing/contradictory
    combination of market_impact="neutral" alongside a non-empty
    impact_windows, or the reverse. Defaults to the keyword-based
    check only when the caller doesn't supply one.
    """
    if is_relevant is None:
        is_relevant = category != "other" or bool(FINANCIAL_CONTEXT_RE.search(title))
    if not is_relevant:
        return []
    if HIGH_SEVERITY_RE.search(title):
        return ["1m", "5m", "15m"]
    if category in ("geopolitical", "economics", "energy"):
        return ["5m", "15m"]
    if category in ("banking", "mergers", "auto", "tech", "market"):
        return ["15m"]
    return []


def dedupe_key(title):
    """Normalize a headline to a stable signature for de-dup -- strips
    punctuation/case so trivially-reworded repeats of the same story
    still match."""
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()[:120]


# 2026-07-27 — real limitation raised directly: pure keyword matching
# can never distinguish "war mentioned as the actual bearish subject"
# from "war mentioned in passing while the headline's real content is
# a bullish stock recommendation" — e.g. "Stocks to buy under ₹200:
# Amid escalation in US-Iran war, [analyst] recommends three shares to
# buy" gets flagged BEARISH by BEARISH_WORDS_RE purely because "war"
# appears in the text, even though the headline is literally a BUY
# recommendation. No amount of keyword-list tuning fixes this — it's a
# structural limitation of bag-of-words matching, not a tunable
# threshold. Real semantic understanding requires actually reading
# what the headline says, which needs an LLM, not another regex.
_ai_classify_cache = {}          # dedupe_key -> (date_str, result_dict)
_ai_classify_calls_today = 0
_ai_classify_day = None


def classify_headline_ai(title, cfg=None):
    """LLM-based semantic classification — the PRIMARY method when
    available, judging the headline's actual substance rather than
    matching individual words. Falls back gracefully (returns
    (None, reason)) when AI is disabled, rate-limited by its own daily
    budget, or the call itself fails or returns something invalid —
    callers use classify_bias()/classify_category() (keyword-based) as
    the safety net in that case, exactly the same "AI first, rule
    engine as fallback" pattern already established for signal
    generation (analyzer.ai_signal()).

    Cached by `dedupe_key()` (the SAME normalization already used for
    story de-duplication) so a repeated/reworded version of a headline
    already classified today doesn't spend another call. Budgeted
    separately from the trading-signal AI budget (`ai_daily_call_cap`)
    since news volume and signal-generation volume are unrelated
    quantities that shouldn't compete for the same cap.

    Returns ({"relevant": bool, "bias": "bullish|bearish|neutral",
    "reasoning": str}, None) on success, or (None, error_string) when
    AI wasn't used for any reason.
    """
    global _ai_classify_calls_today, _ai_classify_day
    import config as _cfg
    cfg = cfg or _cfg.load()
    if not cfg.get("news_ai_classification_enabled", True):
        return None, "ai_classification_disabled"
    if cfg.get("ai_engine", "local") == "off":
        return None, "ai_engine_off"
    key = dedupe_key(title)
    today = time.strftime("%Y-%m-%d")
    if _ai_classify_day != today:
        _ai_classify_day, _ai_classify_calls_today = today, 0
    cached = _ai_classify_cache.get(key)
    if cached and cached[0] == today:
        return cached[1], None
    cap = cfg.get("news_ai_classification_daily_cap", 150)
    if _ai_classify_calls_today >= cap:
        return None, "daily_cap_reached"
    prompt = (
        "You are a financial news analyst for the Indian stock market "
        "(NIFTY/SENSEX/BANKNIFTY/FINNIFTY). Judge this ONE headline's "
        "actual substance and likely directional impact on Indian "
        "equities \u2014 do NOT just react to individual words. A headline "
        "can mention war/crash/conflict/tension only as background "
        "context while its real content is bullish (e.g. a stock BUY "
        "recommendation, an earnings beat, a merger) \u2014 judge what the "
        "headline is actually reporting or recommending, not which "
        "words appear in it.\n"
        "Output ONLY this JSON, nothing else: "
        '{"relevant":true|false,"bias":"bullish|bearish|neutral",'
        '"reasoning":"<=15 words"}\n'
        "Headline: " + title[:200]
    )
    try:
        import llm
        text, engine, err = llm.generate_json(prompt, 150)
    except Exception as e:
        return None, f"call failed: {type(e).__name__}: {e}"
    _ai_classify_calls_today += 1
    if err:
        return None, err
    try:
        d = json.loads(text)
    except Exception as e:
        return None, f"invalid JSON from AI: {e}"
    # Same "validate before trusting" discipline already applied after
    # a real bug where an AI signal response echoed back its own
    # prompt placeholder instead of choosing a real value — never
    # accept an unvalidated field as if it were guaranteed correct.
    if d.get("bias") not in ("bullish", "bearish", "neutral"):
        return None, f"invalid bias value from AI: {d.get('bias')!r}"
    if not isinstance(d.get("relevant"), bool):
        return None, f"invalid relevant value from AI: {d.get('relevant')!r}"
    result = {"relevant": d["relevant"], "bias": d["bias"],
             "reasoning": str(d.get("reasoning", ""))[:200]}
    _ai_classify_cache[key] = (today, result)
    return result, None


# ------------------------------------------------------------ RSS fetch

def fetch_rss(url, timeout=10, max_items=20):
    """Fetch and parse an RSS 2.0 / Atom feed. Returns a list of
    {"title": str, "link": str, "published": str|None} dicts. Raises
    on network/parse failure -- callers decide whether to log-and-skip
    or propagate, matching broker_adapter.py's convention of not
    swallowing errors silently."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    root = ET.fromstring(raw)
    items = []
    channel_items = root.findall(".//item")
    if channel_items:
        for it in channel_items[:max_items]:
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate")
            if title:
                items.append({"title": title, "link": link, "published": pub})
    else:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for it in root.findall(".//atom:entry", ns)[:max_items]:
            title = (it.findtext("atom:title", namespaces=ns) or "").strip()
            link_el = it.find("atom:link", ns)
            link = link_el.get("href") if link_el is not None else ""
            pub = it.findtext("atom:updated", namespaces=ns)
            if title:
                items.append({"title": title, "link": link, "published": pub})
    return items


def test_feed(url, timeout=10):
    """Validate a feed URL, returning a diagnostic dict rather than
    raising -- used by /api/news/feeds/test so adding a new source
    gives an actionable pass/fail instead of a stack trace."""
    try:
        items = fetch_rss(url, timeout=timeout, max_items=5)
        if not items:
            return {"ok": False, "error": "Feed fetched but contained no "
                    "items -- check the URL is a direct feed link, not a "
                    "webpage that merely links to one."}
        return {"ok": True, "sample_items": items,
               "message": f"Fetched {len(items)} sample item(s) successfully."}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code} -- {e.reason}"}
    except urllib.error.URLError as e:
        return {"ok": False, "error": f"Network error: {e.reason}"}
    except ET.ParseError as e:
        return {"ok": False, "error": f"Not valid XML/RSS: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


RELEVANCE_VETOES = []          # in-process ring, newest last
_VETO_MAX = 500


def _log_relevance_veto(title, category, ai_result):
    """Record every AI-relevance veto so the recall cost is MEASURED.

    v59.0 item 39. A veto that is silent is a veto nobody can audit: if
    this filter is throwing away genuinely relevant headlines, the only
    way to find out is to look at what it threw away. Kept in memory
    (bounded) and surfaced through the news payload rather than written
    to yet another file.
    """
    RELEVANCE_VETOES.append({
        "ts": time.time(), "title": title, "category": category,
        "ai_bias": ai_result.get("bias"),
        "ai_reasoning": ai_result.get("reasoning", ""),
    })
    if len(RELEVANCE_VETOES) > _VETO_MAX:
        del RELEVANCE_VETOES[:-_VETO_MAX]


def veto_stats():
    """(count, recent) — for the dashboard and for a human spot-check."""
    return {"vetoed": len(RELEVANCE_VETOES), "recent": RELEVANCE_VETOES[-25:]}


def process_item(item, source_name, region="india"):
    """Turn a raw RSS item into the unified event record this module
    produces for both consumers.

    2026-07-27 — real gap found: `valid` only ever checked that the
    title was non-empty — there was no actual RELEVANCE check at all.
    A completely unrelated headline could still get a real bias label
    if it happened to contain a generic bearish/bullish word (e.g.
    "fall" in a weather report). Fixed once with a broader keyword-
    context check (FINANCIAL_CONTEXT_RE) — but keyword matching alone
    has a deeper structural limit no threshold tuning can fix: it
    cannot tell "war mentioned as the actual bearish subject" apart
    from "war mentioned in passing while the headline is really a
    bullish stock recommendation." Now tries AI semantic
    classification FIRST (classify_headline_ai — judges the headline's
    actual substance, not its words) and falls back to the keyword-
    based approach only when AI is unavailable for any reason (same
    "AI first, rule engine as fallback" pattern already established
    for trading-signal generation).
    """
    title = item["title"][:200]
    category = classify_category(title)
    ai_result, ai_error = classify_headline_ai(title)
    # v59.0 item 39 — KEYWORD RELEVANCE IS A VETO, NOT A FALLBACK.
    #
    # 2026-08-01: the AI path trusted `ai_result["relevant"]` outright, so
    # a local 3B model could UPGRADE a headline with zero financial signal.
    # Observed: "Local team's morale suffers crash after tough loss" came
    # back relevant=True, bias=bearish, reasoning "Negative headline
    # suggesting local team performance impact on investor sentiment" —
    # nonsense that then earned the full [1m,5m,15m] impact window and fed
    # the `news` bus key that gates strategy decisions.
    #
    # Now the AI may DOWNGRADE relevance but never upgrade a headline that
    # is category "other" AND matches no financial context. Same veto-only
    # shape as basis_residual.gate_for(): it can stop something, never
    # cause it.
    #
    # This costs AI recall on genuinely relevant headlines phrased without
    # any financial keyword. Accepted deliberately, because the harms are
    # asymmetric — a false "Risk" read gates real trades, a missed story
    # does not — and every veto is LOGGED below so the recall loss is
    # measured rather than assumed.
    keyword_relevant = (category != "other"
                        or bool(FINANCIAL_CONTEXT_RE.search(title)))
    if ai_result is not None:
        is_relevant = bool(ai_result["relevant"]) and keyword_relevant
        if ai_result["relevant"] and not keyword_relevant:
            _log_relevance_veto(title, category, ai_result)
        bias = ai_result["bias"] if is_relevant else "neutral"
        classification_source = "ai"
        classification_note = ai_result.get("reasoning", "")
        if not is_relevant and ai_result["relevant"]:
            classification_note = (f"AI said relevant ({classification_note}) "
                                   f"but no financial context — VETOED")
    else:
        # Fallback: keyword-based, with the relevance gate this same
        # fix pass added — an irrelevant headline is forced to neutral
        # rather than trusting a bare word match.
        is_relevant = category != "other" or bool(FINANCIAL_CONTEXT_RE.search(title))
        bias = classify_bias(title) if is_relevant else "neutral"
        classification_source = "keyword"
        classification_note = f"AI unavailable: {ai_error}" if ai_error else ""
    # Explicitly threaded through rather than re-derived — see
    # classify_impact_window's own docstring for why: this function
    # would otherwise independently re-decide relevance via keywords
    # even when AI already made a more authoritative call, producing a
    # contradictory combination (e.g. market_impact="neutral" alongside
    # a non-empty impact_windows).
    windows = classify_impact_window(title, category, is_relevant)
    valid = bool(title.strip())
    return {
        "source": source_name, "description": title, "link": item.get("link", ""),
        "category": category, "market_impact": bias, "impact_windows": windows,
        "region": region, "valid": valid,
        "action": ("monitor" if bias != "neutral" and windows else "none"),
        "fetched_ts": time.time(),
        "classification_source": classification_source,
        "classification_note": classification_note,
    }


# --------------------------------------------------------- feed CRUD

def load_feeds():
    if os.path.exists(FEEDS_FILE):
        try:
            with open(FEEDS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return list(DEFAULT_FEEDS)


def save_feeds(feeds):
    os.makedirs(STORE_DIR, exist_ok=True)
    tmp = FEEDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(feeds, f, indent=2)
    os.replace(tmp, FEEDS_FILE)


def add_feed(name, url, category, region, feed_id=None):
    feeds = load_feeds()
    feed_id = feed_id or re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if any(f["id"] == feed_id for f in feeds):
        raise ValueError(f"a feed with id {feed_id!r} already exists")
    feeds.append({"id": feed_id, "name": name, "url": url,
                 "category": category, "region": region, "enabled": True})
    save_feeds(feeds)
    return feed_id


def delete_feed(feed_id):
    feeds = load_feeds()
    remaining = [f for f in feeds if f["id"] != feed_id]
    if len(remaining) == len(feeds):
        raise ValueError(f"no feed with id {feed_id!r}")
    save_feeds(remaining)


def fetch_all_enabled(max_items_per_feed=10):
    """Fetch every enabled feed, returning (events, errors) -- errors
    are per-feed and logged-not-swallowed."""
    events, errors = [], []
    for feed in load_feeds():
        if not feed.get("enabled", True):
            continue
        try:
            items = fetch_rss(feed["url"], max_items=max_items_per_feed)
            for it in items:
                events.append(process_item(it, feed["name"], feed.get("region", "india")))
        except Exception as e:
            errors.append({"feed": feed["name"], "id": feed["id"], "error": str(e)})
    return events, errors


# ---------------------------------------------------- shared dedup + tracker

TRACKER_FILE = os.path.join(STORE_DIR, "news_tracker.jsonl")
_SEEN_HEADLINES = {}    # dedupe_key -> last_logged_ts (module-level, shared
                        # by every caller in this process -- NewsAgent and
                        # NewsMacroAgent are threads in the SAME process, so
                        # this is genuinely shared state, not per-agent)
_SEEN_TTL_SECONDS = 6 * 3600   # a story re-appearing (reworded) within 6h
                               # is treated as the same story, not new


def is_duplicate(title):
    """Check + record in one call -- returns True if this exact
    normalized headline was already logged within the TTL window
    (across EITHER agent), False if this is genuinely new (and records
    it as seen going forward)."""
    key = dedupe_key(title)
    now = time.time()
    last_seen = _SEEN_HEADLINES.get(key)
    if last_seen and (now - last_seen) < _SEEN_TTL_SECONDS:
        return True
    _SEEN_HEADLINES[key] = now
    if len(_SEEN_HEADLINES) > 2000:   # bounded growth -- prune stale entries
        cutoff = now - _SEEN_TTL_SECONDS
        for k in [k for k, ts in _SEEN_HEADLINES.items() if ts < cutoff]:
            del _SEEN_HEADLINES[k]
    return False


def log_tracked_event(event):
    """Append one processed event to the shared tracker file, skipping
    if it's a duplicate of something already logged (by either agent)
    within the TTL. Returns True if actually logged, False if skipped
    as a duplicate."""
    if is_duplicate(event["description"]):
        return False
    os.makedirs(STORE_DIR, exist_ok=True)
    with open(TRACKER_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
    return True


def read_tracked_events(limit=200):
    """Most-recent-first list of tracked events for the dashboard's
    news tracker table. Explicitly sorted by fetched_ts (2026-07-24 —
    was relying on file-append order + reversal, which is only a
    reliable proxy for recency if writes are strictly serialized; this
    file is now written by BOTH NewsAgent and NewsMacroAgent from
    separate threads, so an explicit timestamp sort is the correct,
    robust approach rather than assuming append order)."""
    if not os.path.exists(TRACKER_FILE):
        return []
    # Read more than `limit` raw lines since sorting could reorder
    # which ones are actually the most recent `limit` after sorting —
    # reading a generous multiple keeps this correct without reading
    # the entire (possibly large) file on every call.
    with open(TRACKER_FILE, encoding="utf-8") as f:
        lines = f.readlines()[-(limit * 3):]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    events.sort(key=lambda e: e.get("fetched_ts", 0), reverse=True)
    return events[:limit]


def prune_tracker_file(max_age_hours=48):
    """Drop tracker entries older than max_age_hours (default 48h = 2
    market days, per explicit user request — was 120h/5 days). Called
    periodically from NewsAgent.cycle(), not on every write, to keep
    this cheap. NOTE: this function existed but was never actually
    called anywhere before 2026-07-24 — meaning retention was
    unbounded in practice despite this being built. Confirmed live:
    the tracker file had accumulated 1000+ entries. Now wired in."""
    if not os.path.exists(TRACKER_FILE):
        return
    cutoff = time.time() - max_age_hours * 3600
    kept = []
    with open(TRACKER_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                evt = json.loads(line)
                if evt.get("fetched_ts", 0) >= cutoff:
                    kept.append(line)
            except json.JSONDecodeError:
                continue
    tmp = TRACKER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(kept)
    os.replace(tmp, TRACKER_FILE)
