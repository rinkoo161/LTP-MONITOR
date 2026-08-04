"""IST clock + NSE market calendar.

The single definition of "is the market open" for the whole system —
ltp-monitor was bitten by two drifting market-session checks and collapsed
them to one; this repo starts with one.

Sessions (capital market segment):
    pre-open   09:00–09:08 (order entry; 09:08–09:15 matching/buffer)
    regular    09:15–15:30
    closing    15:40–16:00 (post-close session; thin, rarely matters here)

Holiday data: the static seed below covers 2024–2026 trading holidays for
the equity segment. 2026 dates past May 2026 were written from the NSE
circular known at build time — `refresh_holidays()` pulls the live
holiday-master API through the shared NSEClient and OVERRIDES the seed, so
the seed is a bootstrap, not the long-term truth. Muhurat trading is a
holiday-with-a-special-session: `is_trading_day` is False for it (the
regular session does not run) but `muhurat_date` exposes it for schedulers.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

PRE_OPEN_START = time(9, 0)
REGULAR_START = time(9, 15)
REGULAR_END = time(15, 30)
CLOSING_END = time(16, 0)


class SessionState(str, Enum):
    CLOSED = "closed"            # holiday / weekend / outside hours
    PRE_OPEN = "pre_open"        # 09:00–09:15 on a trading day
    OPEN = "open"                # 09:15–15:30
    POST_CLOSE = "post_close"    # 15:30–16:00


# --- static holiday seed (equity segment trading holidays) -------------------
# YYYY-MM-DD -> name. Weekends are handled separately, so Saturday/Sunday
# holidays are deliberately omitted from the seed even when NSE lists them.
_SEED_HOLIDAYS: dict[str, str] = {
    # 2024
    "2024-01-26": "Republic Day",
    "2024-03-08": "Mahashivratri",
    "2024-03-25": "Holi",
    "2024-03-29": "Good Friday",
    "2024-04-11": "Id-Ul-Fitr",
    "2024-04-17": "Ram Navami",
    "2024-05-01": "Maharashtra Day",
    "2024-05-20": "Lok Sabha Elections (Mumbai polling)",
    "2024-06-17": "Bakri Id",
    "2024-07-17": "Moharram",
    "2024-08-15": "Independence Day",
    "2024-10-02": "Mahatma Gandhi Jayanti",
    "2024-11-01": "Diwali Laxmi Pujan (muhurat only)",
    "2024-11-15": "Gurunanak Jayanti",
    "2024-12-25": "Christmas",
    # 2025
    "2025-02-26": "Mahashivratri",
    "2025-03-14": "Holi",
    "2025-03-31": "Id-Ul-Fitr",
    "2025-04-10": "Shri Mahavir Jayanti",
    "2025-04-14": "Dr. Ambedkar Jayanti",
    "2025-04-18": "Good Friday",
    "2025-05-01": "Maharashtra Day",
    "2025-08-15": "Independence Day",
    "2025-08-27": "Ganesh Chaturthi",
    "2025-10-02": "Mahatma Gandhi Jayanti / Dussehra",
    "2025-10-21": "Diwali Laxmi Pujan (muhurat only)",
    "2025-10-22": "Diwali Balipratipada",
    "2025-11-05": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2025-12-25": "Christmas",
    # 2026 — from the NSE holiday circular available at build time.
    # refresh_holidays() replaces this from the live API; do not hand-extend.
    "2026-01-26": "Republic Day",
    "2026-02-15": "Mahashivratri",  # falls on Sunday; kept for completeness
    "2026-03-03": "Holi",
    "2026-03-21": "Id-Ul-Fitr",     # falls on Saturday
    "2026-03-31": "Shri Mahavir Jayanti",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-27": "Bakri Id",
    "2026-06-26": "Moharram",
    "2026-08-15": "Independence Day",  # falls on Saturday
    "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (muhurat only)",  # Sunday
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Gurunanak Jayanti",
    "2026-12-25": "Christmas",
}

MUHURAT_DATES = {date(2024, 11, 1), date(2025, 10, 21), date(2026, 11, 8)}


@dataclass
class MarketCalendar:
    """Holiday-aware NSE calendar. Instantiate once; refresh from DB/API."""

    holidays: dict[date, str] | None = None

    def __post_init__(self) -> None:
        if self.holidays is None:
            self.holidays = {
                date.fromisoformat(d): name for d, name in _SEED_HOLIDAYS.items()
            }

    # -- day-level ------------------------------------------------------
    def is_trading_day(self, d: date) -> bool:
        if d.weekday() >= 5:  # Sat/Sun
            return False
        return d not in self.holidays

    def holiday_name(self, d: date) -> str | None:
        return self.holidays.get(d)

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def prev_trading_day(self, d: date) -> date:
        prv = d - timedelta(days=1)
        while not self.is_trading_day(prv):
            prv -= timedelta(days=1)
        return prv

    def muhurat_date(self, year: int) -> date | None:
        for d in MUHURAT_DATES:
            if d.year == year:
                return d
        return None

    # -- intraday -------------------------------------------------------
    def session_state(self, ts: datetime | None = None) -> SessionState:
        ts = ts.astimezone(IST) if ts else now_ist()
        if not self.is_trading_day(ts.date()):
            return SessionState.CLOSED
        t = ts.time()
        if PRE_OPEN_START <= t < REGULAR_START:
            return SessionState.PRE_OPEN
        if REGULAR_START <= t < REGULAR_END:
            return SessionState.OPEN
        if REGULAR_END <= t < CLOSING_END:
            return SessionState.POST_CLOSE
        return SessionState.CLOSED

    def is_market_open(self, ts: datetime | None = None) -> bool:
        return self.session_state(ts) == SessionState.OPEN

    def update_holidays(self, holidays: dict[date, str]) -> None:
        """Replace the holiday table (from the NSE holiday-master API)."""
        if not holidays:
            raise ValueError("refusing to replace holiday table with an empty one")
        self.holidays = dict(holidays)


def now_ist() -> datetime:
    return datetime.now(IST)


# Module-level default — the shared instance everything imports.
calendar = MarketCalendar()
