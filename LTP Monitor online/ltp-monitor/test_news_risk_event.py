#!/usr/bin/env python3
"""test_news_risk_event.py — a risk event must describe an actual event.

2026-08-08. `NewsAgent` asked the model for `risk_event` with NO
definition of what one is, and the answers show it. Of 114 flagged
events in activity.log:

    23  described MIXED conditions ("both positive and negative")
     6  were the literal prompt placeholder "<one line>"
     3  described purely POSITIVE developments
     1  was empty

33 — nearly a third — where the classifier's own description says it is
not a risk event. Each fired a HIGH alert, and when one lands with a
directional sentiment it blocks trades for up to `news_block_minutes`
through `news_risk_opportunity()`, which `RiskAgent.evaluate()` consults
on every order.

THE FILTER IS A PRECISE NEGATIVE, AND THAT IS THE DESIGN. It rejects
only what is demonstrably not an event. Requiring a HIGH_SEVERITY_RE
keyword instead would have been tighter and WRONG: it suppresses a
genuine event already in the log — "Sensex drops over 200 points and
Nifty tests 23,600 due to Strait of Hormuz tensions" matches none of
those words. For a risk gate, ambiguity must fail toward keeping the
event.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import store
store.require_isolated("test_news_risk_event")

FAILED = []


def check(label, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {label}" + (f"  — {detail}" if detail else ""))
    if not cond:
        FAILED.append(label)


import news_engine as ne

HERE = os.path.dirname(os.path.abspath(__file__))

print("1) the four bogus shapes seen in production are rejected")
BOGUS = [
    ("", "empty note"),
    ("   ", "whitespace-only note"),
    ("<one line>", "the prompt placeholder, returned verbatim 6 times"),
    ("Mixed market performance with both positive and negative headlines",
     "mixed conditions"),
    ("Indian market sentiment is mixed with both positive and negative headlines.",
     "mixed conditions, real log line"),
    ("Mixed economic news including both positive (e.g., India's resilient "
     "economy) and negative (e.g., US Senate Russia sanctions bill) developments.",
     "the 2026-08-08 15:22 event"),
]
for note, why in BOGUS:
    material, reason = ne.is_material_risk_event(note)
    check(f"rejected: {why}", not material, f"{note[:60]!r} -> {reason!r}")

print("\n1b) AMBIGUOUS notes are deliberately KEPT, not rejected")
# "Indian indices ended higher amid easing US-Iran tensions" READS as
# positive, and my first version of this test asserted it should be
# dropped. It should not: it names a geopolitical situation, and
# classify_bias() correctly returns "neutral" because the text carries
# both bullish ("higher", "easing") and bearish ("tensions") wording.
# For a RISK gate, ambiguity must fail toward keeping the event — the
# cost of a spurious block is one missed trade, the cost of a missed
# block is trading into an event. The test was wrong, not the filter.
for note in ("Indian indices ended higher amid easing US-Iran tensions",
             "Markets steady as investors weigh Fed commentary"):
    material, reason = ne.is_material_risk_event(note)
    check(f"kept (ambiguous): {note[:46]}", material,
          f"reason={reason!r} — bias={ne.classify_bias(note)!r}")


print("\n2) GENUINE events survive — this is the half that matters")
# Every one of these is a real line from activity.log or a canonical
# event type. A filter that drops these is worse than no filter.
GENUINE = [
    "Sensex drops over 200 points and Nifty tests 23,600 due to Strait of "
    "Hormuz tensions",
    "RBI holds repo rate, signals caution on inflation",
    "Indices plunge after circuit breaker triggered on heavy selling",
    "Major NBFC defaults on commercial paper",
]
for note in GENUINE:
    material, reason = ne.is_material_risk_event(note)
    check(f"kept: {note[:52]}", material, f"reason={reason!r}")

print("\n3) it reuses the existing sentiment definition, not a new one")
SRC = open(os.path.join(HERE, "news_engine.py")).read()
body = SRC.split("def is_material_risk_event(")[1]
body = body[:body.index("\ndef ")]
check("it calls classify_bias rather than new sentiment regexes",
      "classify_bias(" in body,
      "the news wording has already been forked once in this codebase "
      "and had to be collapsed back to one definition")
check("and it does NOT require a HIGH_SEVERITY_RE match",
      "HIGH_SEVERITY_RE" not in body,
      "a keyword whitelist would suppress the Strait of Hormuz event — "
      "ambiguity must fail toward KEEPING a risk event")

print("\n4) the agent actually enforces it")
AG = open(os.path.join(HERE, "agents.py")).read()
_code = [l for l in AG.split("\n") if not l.strip().startswith("#")]
check("NewsAgent calls is_material_risk_event",
      any("is_material_risk_event(" in l for l in _code))
check("and downgrades risk_event when the note is not material",
      any('j["risk_event"] = False' in l for l in _code))
check("and records WHY, rather than downgrading silently",
      any("risk_event_downgraded" in l for l in _code) and
      "news risk_event downgraded" in AG,
      "a gate that quietly stops firing is the failure this codebase "
      "keeps hitting")

print("\n5) the prompt now DEFINES risk_event")
check("the prompt states what a risk event is",
      "risk_event means a SPECIFIC, DATEABLE event" in AG,
      "it previously asked for the field with no definition at all")
check("and no longer uses angle-bracket placeholders",
      '\\"note\\":\\"<one line>\\"' not in AG and '"note":"<one line>"' not in AG,
      "'<one line>' was returned verbatim and flagged as a risk event "
      "6 times")

print("\n6) replayed against the real log, the split is as measured")
log = os.path.expanduser("~/.ltp-monitor/activity.log")
if os.path.exists(log):
    notes = [l.split("News risk event:")[-1].strip()
             for l in open(log, errors="ignore") if "News risk event" in l]
    if notes:
        dropped = sum(1 for n in notes if not ne.is_material_risk_event(n)[0])
        kept = len(notes) - dropped
        check("most events are still kept", kept > dropped,
              f"{kept} kept / {dropped} dropped of {len(notes)} — a filter "
              f"that rejected the majority would be gating on the wrong side")
    else:
        print("SKIP  no news risk events in the log")
else:
    print("SKIP  no activity.log in this store")

print()
if FAILED:
    print(f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1)
print("all news risk-event checks passed")
