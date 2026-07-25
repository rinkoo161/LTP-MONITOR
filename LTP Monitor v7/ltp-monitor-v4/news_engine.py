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
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
STORE_DIR = os.path.expanduser("~/.ltp-monitor")
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


def classify_impact_window(title, category):
    """Returns a list of windows this headline is estimated to affect:
    subset of ["1m", "5m", "15m"], or [] if not expected to move price
    at candle level at all."""
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


def process_item(item, source_name, region="india"):
    """Turn a raw RSS item into the unified event record this module
    produces for both consumers."""
    title = item["title"][:200]
    category = classify_category(title)
    bias = classify_bias(title)
    windows = classify_impact_window(title, category)
    valid = bool(title.strip())
    return {
        "source": source_name, "description": title, "link": item.get("link", ""),
        "category": category, "market_impact": bias, "impact_windows": windows,
        "region": region, "valid": valid,
        "action": ("monitor" if bias != "neutral" and windows else "none"),
        "fetched_ts": time.time(),
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
