"""Topic names — the full §3 vocabulary, declared now so producers and
consumers share one constant. Phase 1 only emits FILING_RECEIVED."""

FILING_RECEIVED = "filing.received"        # A1 → A2
FILING_CLASSIFIED = "filing.classified"    # A2 → A3/A7 (Phase 2)
FUNDAMENTAL_UPDATED = "fundamental.updated"  # A3 (Phase 3)
TECHNICAL_UPDATED = "technical.updated"      # A4 (Phase 3)
FLOW_UPDATED = "flow.updated"                # A5 (Phase 3)
RISK_ASSESSED = "risk.assessed"              # A6 (Phase 4)
SIGNAL_ISSUED = "signal.issued"              # A7 (Phase 4)

ALL_TOPICS = [v for k, v in sorted(globals().items()) if k.isupper() and isinstance(v, str)]

# Postgres NOTIFY channel — one channel; consumers filter by topic in SQL.
NOTIFY_CHANNEL = "ms_outbox"
