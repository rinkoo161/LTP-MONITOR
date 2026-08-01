"""v58.9 (part 8, item 10) — tests for the news relevance/spam filter,
which previously only checked non-empty title with no actual relevance
check at all.

Two real bugs found and fixed:
1. `HIGH_SEVERITY_RE` ran unconditionally, before any category or
   relevance check — a totally unrelated headline (sports,
   entertainment) containing a generic severity word ("crash", "war",
   "emergency") got the MOST aggressive 3-window market-impact
   classification regardless of actual financial content.
2. `process_item()`'s `valid` field only checked `bool(title.strip())`
   — genuinely irrelevant stories with a generic bearish/bullish word
   still got a real bias label and, via bug #1, could feed the
   downstream news-macro tracker page with a false "Risk"/"Opportunity"
   read on something with zero market relevance.

Also closes a separate, distinct maintainability gap: news_macro_agent.py
maintained its own byte-identical COPY of BEARISH_WORDS_RE/
BULLISH_WORDS_RE, a genuine "two copies that will silently drift" risk.

Run:  python3 test_news_relevance_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import news_engine as ne
import news_macro_agent as nma

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


# 2026-08-01 (item 39) — THIS FILE USED TO TEST THE MODEL, NOT THE LOGIC.
# process_item() calls classify_headline_ai(); with Ollama running, the
# local 3B model returned relevant=True/bearish for the sports headline
# below and the file failed. With Ollama down it fell back to keywords
# and passed. A test whose verdict depends on whether a daemon is up
# cannot tell a regression from a restart, so the AI is now PINNED and
# each path is exercised explicitly.
_real_ai = ne.classify_headline_ai


def pin_ai(result, error=None):
    """Force classify_headline_ai to a fixed answer."""
    ne.classify_headline_ai = lambda title: (result, error)


def unpin():
    ne.classify_headline_ai = _real_ai


print("1) THE REPORTED BUG PATTERN: an irrelevant headline containing a "
     "generic high-severity word gets neutralized, not the aggressive "
     "3-window classification")
# Pinned to the exact hallucination observed from qwen2.5:3b — the AI
# calls this sports headline relevant AND bearish. The keyword veto must
# override it.
pin_ai({"relevant": True, "bias": "bearish",
        "reasoning": "Negative headline suggesting local team performance "
                     "impact on investor sentiment"})
r1 = ne.process_item({"title": "Local team's morale suffers crash after "
                                "tough loss", "link": ""}, "test_feed")
check("category correctly falls to 'other' (no real financial content)",
      r1["category"] == "other", r1["category"])
check("bias forced to neutral despite containing a bearish-pattern word",
      r1["market_impact"] == "neutral", r1["market_impact"])
check("impact_windows is empty, NOT the aggressive [1m,5m,15m] the raw "
      "severity word would have triggered before this fix",
      r1["impact_windows"] == [], str(r1["impact_windows"]))
check("action correctly stays 'none'", r1["action"] == "none", r1["action"])

print("\n2) a genuinely relevant, high-severity market headline is "
     "UNAFFECTED by the fix — still gets the full aggressive window")
pin_ai({"relevant": True, "bias": "bearish",
        "reasoning": "RBI policy and geopolitical risk, directly market-moving"})
r2 = ne.process_item({"title": "RBI rate decision sends Sensex into "
                                "crash amid war fears", "link": ""}, "test_feed")
check("category correctly identified", r2["category"] != "other", r2["category"])
check("bias correctly bearish", r2["market_impact"] == "bearish", r2["market_impact"])
check("full 3-window severity classification preserved for a "
      "genuinely severe, relevant story",
      r2["impact_windows"] == ["1m", "5m", "15m"], str(r2["impact_windows"]))
check("action correctly 'monitor'", r2["action"] == "monitor", r2["action"])

print("\n3) an irrelevant headline with only a MILD generic bearish "
     "word (no severity trigger at all) is also correctly neutralized")
r3 = ne.process_item({"title": "Actor career sees weak reviews after "
                                "latest film", "link": ""}, "test_feed")
check("bias forced to neutral", r3["market_impact"] == "neutral", r3["market_impact"])
check("action stays none", r3["action"] == "none", r3["action"])

print("\n4) relevance via the BROADER financial-context regex (not a "
     "specific CATEGORY hit) still correctly registers as relevant")
r4 = ne.process_item({"title": "Company shares fall after weak "
                                "quarterly earnings", "link": ""}, "test_feed")
check("bias correctly computed as bearish (relevant via broad "
      "financial context, even though no specific CATEGORY matched)",
      r4["market_impact"] == "bearish", r4["market_impact"])

print("\n5) FINANCIAL_CONTEXT_RE itself behaves sensibly on direct cases")
check("matches generic market vocabulary", bool(ne.FINANCIAL_CONTEXT_RE.search(
      "Investors watch the market closely")))
check("does NOT match a genuinely unrelated sentence",
      not bool(ne.FINANCIAL_CONTEXT_RE.search(
          "The weather today is sunny with light clouds")))

print("\n6) regex deduplication: news_macro_agent no longer maintains "
     "its own copy — confirmed to be the LITERAL SAME object as "
     "news_engine's, not just an equal one")
check("BEARISH_WORDS_RE is the identical object (not a parallel copy "
      "that could silently drift)",
      nma.BEARISH_WORDS_RE is ne.BEARISH_WORDS_RE)
check("BULLISH_WORDS_RE is the identical object",
      nma.BULLISH_WORDS_RE is ne.BULLISH_WORDS_RE)
check("existing classify_bias() functionality in news_macro_agent is "
      "unaffected by the dedup",
      nma.classify_bias("Sensex falls sharply amid weak sentiment") == "bearish" and
      nma.classify_bias("Markets rally as growth outlook improves") == "bullish")

print("\n7) source-level guard: the duplicate regex definitions are "
     "actually gone from news_macro_agent.py, not just aliased "
     "alongside a leftover copy")
src = open("news_macro_agent.py").read()
check("no separate re.compile call for BEARISH_WORDS_RE remains",
      "BEARISH_WORDS_RE = re.compile(" not in src)
check("no separate re.compile call for BULLISH_WORDS_RE remains",
      "BULLISH_WORDS_RE = re.compile(" not in src)
check("both are now assigned directly from news_engine",
      "BEARISH_WORDS_RE = ne.BEARISH_WORDS_RE" in src and
      "BULLISH_WORDS_RE = ne.BULLISH_WORDS_RE" in src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
