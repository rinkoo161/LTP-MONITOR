"""The 23 NSE RSS feeds — the §2.1 table as code.

Poll cadence by priority (seconds):
                 market hours     off hours
    P0           60               300
    P1           300              900
    P2           3600             3600

Live-verified feed shape (2026-08-05): every feed follows
    <item><title>Company Name</title>
          <link>attachment URL (pdf/xml/zip on the archives host)</link>
          <pubDate>DD-Mon-YYYY HH:MM:SS</pubDate>  (IST, no tz suffix)
          <description>K:V fields separated by ' |'</description>
The title is the COMPANY NAME, not the symbol. The symbol, when present,
is the first token of the attachment filename (BHARTIARTL_0508...pdf);
XBRL result files (INDAS_...xml) carry no symbol and resolve by name.
"""
from __future__ import annotations

from dataclasses import dataclass

RSS_BASE = "https://nsearchives.nseindia.com/content/RSS/"


@dataclass(frozen=True)
class FeedSpec:
    name: str        # our stable identifier (db key, metric label)
    filename: str    # URL suffix under RSS_BASE
    priority: str    # P0 | P1 | P2

    @property
    def url(self) -> str:
        return RSS_BASE + self.filename


FEEDS: list[FeedSpec] = [
    FeedSpec("announcements",            "Online_announcements.xml",         "P0"),
    FeedSpec("financial_results",        "Financial_Results.xml",            "P0"),
    FeedSpec("board_meetings",           "Board_Meetings.xml",               "P0"),
    FeedSpec("corporate_actions",        "Corporate_action.xml",             "P0"),
    FeedSpec("insider_trading",          "InsiderTrading.xml",               "P0"),
    FeedSpec("sast_reg29",               "Sast_Regulation29.xml",            "P1"),
    FeedSpec("sast_reg31",               "Sast_Regulation31.xml",            "P1"),
    FeedSpec("encumbrance",              "Sast_ReasonForEncumbrance.xml",    "P0"),  # pledge = red flag
    FeedSpec("shareholding_pattern",     "Shareholding_Pattern.xml",         "P1"),
    FeedSpec("related_party",            "Related_Party_Trans.xml",          "P0"),  # governance
    FeedSpec("buyback",                  "Daily_Buyback.xml",                "P1"),
    FeedSpec("integrated_financials",    "Integrated_Filing_Financials.xml", "P1"),
    FeedSpec("annual_reports",           "Annual_Reports.xml",               "P2"),
    FeedSpec("corporate_governance",     "Corporate_Governance.xml",         "P2"),
    FeedSpec("secretarial_compliance",   "Secretarial_Compliance.xml",       "P2"),
    FeedSpec("deviation_variation",      "Statement_Of_Deviation.xml",       "P1"),
    FeedSpec("investor_complaints",      "Investor_Complaints.xml",          "P2"),
    FeedSpec("voting_results",           "Voting_Results.xml",               "P2"),
    FeedSpec("share_transfers",          "Share_Transfers.xml",              "P2"),
    FeedSpec("offer_documents",          "Offer_Documents.xml",              "P2"),
    FeedSpec("brsr",                     "brsr.xml",                         "P2"),
    FeedSpec("unitholding_patterns",     "Unitholding_Patterns.xml",         "P2"),
    FeedSpec("circulars",                "Circulars.xml",                    "P1"),
]

assert len(FEEDS) == 23, "the brief specifies 23 feeds; keep this list complete"

POLL_INTERVAL = {  # (market_hours_sec, off_hours_sec)
    "P0": (60, 300),
    "P1": (300, 900),
    "P2": (3600, 3600),
}


def by_name(name: str) -> FeedSpec:
    for f in FEEDS:
        if f.name == name:
            return f
    raise KeyError(name)
