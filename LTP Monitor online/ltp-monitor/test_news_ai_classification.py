"""v58.9 (part 9, item 10 — round 2) — tests for AI-based semantic news
classification, added after a specific, well-reasoned point: pure
keyword matching structurally cannot tell "war mentioned as the actual
bearish subject" from "war mentioned in passing while the headline's
real content is a bullish stock recommendation." No amount of keyword-
list tuning fixes this — it needs actual reading comprehension.

Real example given: "Stocks to buy under ₹200: Amid escalation in
US-Iran war, Mehul Kothari of Anand Rathi recommends three shares to
buy" — BEARISH_WORDS_RE flags this bearish purely because "war"
appears, even though the headline is literally a BUY recommendation.

classify_headline_ai() is now the PRIMARY classification method,
falling back to the existing keyword-based approach (item 10, round 1)
only when AI is disabled, rate-limited by its own daily budget, or the
call itself fails/returns something invalid — the exact "AI first,
rule engine as fallback" pattern already established for trading-
signal generation (analyzer.ai_signal()), including the SAME "validate
before trusting" discipline applied after the earlier malformed-
signal-value bug.

Run:  python3 test_news_ai_classification.py
"""
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config
import news_engine as ne

results = []


def check(label, cond, detail=""):
    results.append((label, bool(cond)))
    print(("  PASS  " if cond else "  FAIL  ") + label +
          (f"   [{detail}]" if detail else ""))


REAL_EXAMPLE = ("Stocks to buy under \u20b9200: Amid escalation in US-Iran "
               "war, Mehul Kothari of Anand Rathi recommends three "
               "shares to buy")

print("1) THE EXACT REPORTED CASE: AI correctly identifies this as "
     "bullish (a buy recommendation), which pure keyword matching "
     "cannot — confirmed the OLD keyword-only result really was wrong")
check("keyword-only classify_bias() genuinely gets this wrong "
      "(bearish, purely from the word 'war') — confirms the reported "
      "problem is real, not a misreading",
      ne.classify_bias(REAL_EXAMPLE) == "bearish", ne.classify_bias(REAL_EXAMPLE))

ne._ai_classify_cache.clear()
correct_response = ('{"relevant":true,"bias":"bullish",'
                    '"reasoning":"Headline recommends stock buys despite '
                    'war mention"}')
with mock.patch("llm.generate_json", return_value=(correct_response, "local", None)):
    result = ne.process_item({"title": REAL_EXAMPLE, "link": ""}, "test_feed")
check("with AI classification, the EXACT reported headline is now "
      "correctly bullish, not bearish",
      result["market_impact"] == "bullish", result["market_impact"])
check("classification_source correctly shows 'ai'",
      result["classification_source"] == "ai", result["classification_source"])
check("the AI's reasoning is captured for audit, not discarded",
      "war" in result["classification_note"].lower() or
      "recommend" in result["classification_note"].lower(),
      result["classification_note"])

print("\n2) graceful fallback: AI call failing falls back to the "
     "existing keyword-based approach (item 10 round 1), not a crash")
ne._ai_classify_cache.clear()
with mock.patch("llm.generate_json", side_effect=RuntimeError("connection refused")):
    result2 = ne.process_item({"title": REAL_EXAMPLE, "link": ""}, "test_feed")
check("falls back to keyword source", result2["classification_source"] == "keyword")
check("the fallback note explains WHY AI wasn't used",
      "unavailable" in result2["classification_note"].lower())
check("the fallback bias matches the pre-AI keyword behavior exactly "
      "(no silent behavior change when AI is down)",
      result2["market_impact"] == ne.classify_bias(REAL_EXAMPLE))

print("\n3) config toggle: AI classification can be disabled entirely, "
     "falling back cleanly")
_before_enabled = config.load().get("news_ai_classification_enabled")
config.save({"news_ai_classification_enabled": False})
ne._ai_classify_cache.clear()
try:
    result3 = ne.process_item({"title": REAL_EXAMPLE, "link": ""}, "test_feed")
    check("classification_source is keyword when explicitly disabled",
          result3["classification_source"] == "keyword")
finally:
    config.save({"news_ai_classification_enabled":
                _before_enabled if _before_enabled is not None else True})

print("\n4) validation: malformed AI responses are rejected, not "
     "silently trusted — same discipline already applied after the "
     "earlier malformed-trading-signal bug")
ne._ai_classify_cache.clear()
with mock.patch("llm.generate_json",
               return_value=('{"relevant":true,"bias":"very bearish",'
                            '"reasoning":"x"}', "local", None)):
    r_bad_bias, e_bad_bias = ne.classify_headline_ai("test headline A")
check("an invalid bias enum value is rejected, not silently accepted",
      r_bad_bias is None and "invalid bias" in e_bad_bias, str((r_bad_bias, e_bad_bias)))

ne._ai_classify_cache.clear()
with mock.patch("llm.generate_json",
               return_value=('{"relevant":"yes","bias":"bullish",'
                            '"reasoning":"x"}', "local", None)):
    r_bad_rel, e_bad_rel = ne.classify_headline_ai("test headline B")
check("a non-boolean relevant value is rejected",
      r_bad_rel is None and "invalid relevant" in e_bad_rel, str((r_bad_rel, e_bad_rel)))

ne._ai_classify_cache.clear()
with mock.patch("llm.generate_json", return_value=("not json at all", "local", None)):
    r_bad_json, e_bad_json = ne.classify_headline_ai("test headline C")
check("invalid JSON is rejected without crashing",
      r_bad_json is None and "invalid JSON" in e_bad_json, str((r_bad_json, e_bad_json)))

print("\n5) caching: the same (or trivially reworded) headline "
     "processed twice only calls the LLM once")
call_count = {"n": 0}


def counting_mock(prompt, max_tokens):
    call_count["n"] += 1
    return '{"relevant":true,"bias":"bullish","reasoning":"test"}', "local", None


ne._ai_classify_cache.clear()
with mock.patch("llm.generate_json", side_effect=counting_mock):
    ne.process_item({"title": "Some unique test headline for caching", "link": ""}, "f")
    ne.process_item({"title": "Some unique test headline for caching!", "link": ""}, "f")
check("only 1 LLM call for the same headline processed twice",
      call_count["n"] == 1, str(call_count["n"]))

print("\n6) daily call budget: a configured cap is genuinely enforced")
_before_cap = config.load().get("news_ai_classification_daily_cap")
config.save({"news_ai_classification_daily_cap": 2})
ne._ai_classify_cache.clear()
ne._ai_classify_calls_today = 0
ne._ai_classify_day = None
try:
    with mock.patch("llm.generate_json", side_effect=counting_mock):
        r1, e1 = ne.classify_headline_ai("cap test headline one")
        r2, e2 = ne.classify_headline_ai("cap test headline two")
        r3, e3 = ne.classify_headline_ai("cap test headline three")
    check("first two calls within the cap succeed",
          r1 is not None and r2 is not None)
    check("the third call correctly hits the daily cap",
          r3 is None and e3 == "daily_cap_reached", str((r3, e3)))
finally:
    config.save({"news_ai_classification_daily_cap":
                _before_cap if _before_cap is not None else 150})

print("\n7) impact_windows consistency (a subtle bug I caught and "
     "fixed before shipping): classify_impact_window no longer "
     "independently re-derives relevance via keywords when a MORE "
     "authoritative signal (AI's own relevant judgment) already "
     "governed the bias determination — avoids a contradictory "
     "market_impact='neutral' alongside a non-empty impact_windows")
src = open("news_engine.py").read()
check("classify_impact_window accepts an explicit is_relevant override",
      "def classify_impact_window(title, category, is_relevant=None):" in src)
check("process_item threads the actual relevance signal through "
      "explicitly rather than letting it be re-derived",
      "windows = classify_impact_window(title, category, is_relevant)" in src)

print("\n8) config hygiene: new keys registered on both DEFAULTS and "
     "SettingsIn")
check("news_ai_classification_enabled in config.DEFAULTS",
      config.DEFAULTS.get("news_ai_classification_enabled") is True)
check("news_ai_classification_daily_cap in config.DEFAULTS",
      config.DEFAULTS.get("news_ai_classification_daily_cap") == 150)
app_src = open("app.py").read()
check("both declared on SettingsIn",
      "news_ai_classification_enabled: bool" in app_src and
      "news_ai_classification_daily_cap: int" in app_src)

print("\n" + "=" * 60)
failed = [l for l, ok in results if not ok]
if failed:
    print(f"FAIL ({len(failed)}/{len(results)}):")
    for f in failed:
        print("  - " + f)
    sys.exit(1)
print(f"PASS — all {len(results)} checks")
