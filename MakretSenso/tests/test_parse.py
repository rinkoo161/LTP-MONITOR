"""Feed parsing against fixture bytes built from LIVE-OBSERVED shapes
(2026-08-05) — per the evidence standards: a test that invents its own
input can't catch a producer mismatch, so these fixtures replicate real
NSE payloads verbatim."""
from marketsense.agents.a1_ingestion.parse import (
    content_hash,
    event_at_from_link,
    parse_feed,
    parse_pub,
    split_summary_fields,
    symbol_from_link,
)

# Verbatim structure from Online_announcements.xml, 2026-08-05
RSS_FIXTURE = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>NSE News - Latest Announcements</title>
<item>
<title>Bharti Airtel Limited</title>
<link>https://nsearchives.nseindia.com/corporate/BHARTIARTL_05082026000335_AirtelStxMonitoringAgencyReport.pdf</link>
<pubDate>05-Aug-2026 00:03:49</pubDate>
<description>Monitoring Agency Report for the quarter ended June 30, 2026. |SUBJECT: Monitoring Agency Report</description>
</item>
<item>
<title>V.S.T Tillers Tractors Limited</title>
<link>https://archives.nseindia.com/corporate/xbrl/INDAS_121277_1705282_30072026051753.xml</link>
<pubDate>30-Jul-2026 17:17:53</pubDate>
<description>RELATING TO:Third Quarter |AUDITED/UNAUDITED:Unaudited |PERIOD:Quarterly</description>
</item>
</channel></rss>"""


def test_parse_feed_shape():
    entries = parse_feed("announcements", RSS_FIXTURE)
    assert len(entries) == 2
    e = entries[0]
    assert e["company_title"] == "Bharti Airtel Limited"
    assert e["symbol_hint"] == "BHARTIARTL"
    assert e["event_at"].hour == 0 and e["event_at"].minute == 3
    assert e["subject"] == "Monitoring Agency Report"
    assert e["dedup_key"] == e["link"]
    # XBRL link carries no symbol
    assert entries[1]["symbol_hint"] is None


def test_symbol_from_link_handles_special_symbols():
    assert symbol_from_link(
        "https://nsearchives.nseindia.com/corporate/M&M_01082026120000_x.pdf") == "M&M"
    assert symbol_from_link(
        "https://nsearchives.nseindia.com/corporate/BAJAJ-AUTO_01082026120000_x.pdf"
    ) == "BAJAJ-AUTO"
    assert symbol_from_link(None) is None


def test_parse_pub_formats():
    assert parse_pub("05-Aug-2026 00:03:49").year == 2026
    assert parse_pub("30-Jul-2026 17:17").minute == 17
    assert parse_pub("") is None
    assert parse_pub(None) is None


def test_event_at_from_link_both_shapes():
    # BRSR shape: yyyymmdd _ HHMMSS(fff)
    ts = event_at_from_link(
        "https://nsearchives.nseindia.com/corporate/xbrl/BRSR_17585_WebXMLFile_20260804_233837946.xml")
    assert (ts.year, ts.month, ts.day, ts.hour) == (2026, 8, 4, 23)
    # Annual report shape: ddmmyyyyHHMMSS
    ts = event_at_from_link(
        "https://nsearchives.nseindia.com/annual_reports/AR_30197_MAZDOCK_2025_2026_A_10030945_04082026232241.pdf")
    assert (ts.year, ts.month, ts.day, ts.hour, ts.minute) == (2026, 8, 4, 23, 22)
    assert event_at_from_link("https://x/no_timestamp_here.pdf") is None


def test_split_summary_fields():
    f = split_summary_fields(
        "Monitoring Agency Report for Q1. |SUBJECT: Monitoring Agency Report")
    assert f["SUBJECT"] == "Monitoring Agency Report"
    assert "Monitoring Agency Report for Q1." in f["_text"]
    f2 = split_summary_fields("RELATING TO:Third Quarter |AUDITED/UNAUDITED:Unaudited")
    assert f2["RELATING TO"] == "Third Quarter"
    assert f2["AUDITED/UNAUDITED"] == "Unaudited"


def test_content_hash_stable_and_distinct():
    a = content_hash("f", "t", "l", "p", "s")
    assert a == content_hash("f", "t", "l", "p", "s")
    assert a != content_hash("f2", "t", "l", "p", "s")
    # separator prevents field-boundary collisions
    assert content_hash("f", "ab", "c", "", "") != content_hash("f", "a", "bc", "", "")
