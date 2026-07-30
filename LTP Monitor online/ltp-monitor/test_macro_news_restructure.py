"""v58.24 — SUPERSEDED, kept as a note rather than silently deleted.

This file originally tested the FIRST round of Macro/News
restructuring: an 8/4 grid pairing the wide Macro Event Log table
(#macroTable) and News Tracker (#newsTrackerTable) as two SEPARATE
tables, with Digest and Global Markets Snapshot (#macroMarketData) as
two separate panels in the narrower column.

Per a direct follow-up request in the very next round, those two
pairs were each MERGED further: Macro Event Log + News Tracker became
one ranked table (#macroNewsTable), and Digest + Global Markets
Snapshot became one panel (Digest absorbing the unique pieces of the
other). #macroTable, #newsTrackerTable, and #macroMarketData no longer
exist — this file's own assertions about them no longer apply to
anything that exists.

Superseded by test_macro_news_merge.py, which verifies the current
(merged) structure directly: the ranked combined table, the absorbed
Digest panel, and the RSS Feed Sources scroll-height change from the
same round.

Run:  python3 test_macro_news_restructure.py  (no-op, kept for history)
"""
print("SUPERSEDED by test_macro_news_merge.py — see this file's own "
     "docstring for what changed and why. No checks run here.")
print("PASS — 0 checks (intentionally retired, not a live suite member)")
