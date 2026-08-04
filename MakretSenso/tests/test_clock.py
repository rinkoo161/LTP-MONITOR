from datetime import date, datetime

from marketsense.core.clock import IST, MarketCalendar, SessionState

cal = MarketCalendar()


def test_weekends_closed():
    assert not cal.is_trading_day(date(2026, 8, 1))  # Saturday
    assert not cal.is_trading_day(date(2026, 8, 2))  # Sunday
    assert cal.is_trading_day(date(2026, 8, 3))      # Monday


def test_known_holidays():
    assert not cal.is_trading_day(date(2026, 1, 26))   # Republic Day
    assert not cal.is_trading_day(date(2025, 10, 21))  # Diwali muhurat day
    assert not cal.is_trading_day(date(2024, 3, 29))   # Good Friday
    assert cal.holiday_name(date(2026, 1, 26)) == "Republic Day"


def test_next_prev_trading_day_skips_weekend_and_holiday():
    # Fri 2026-01-23 → next is Tue 2026-01-27 (26th is Republic Day, Mon)
    assert cal.next_trading_day(date(2026, 1, 23)) == date(2026, 1, 27)
    assert cal.prev_trading_day(date(2026, 1, 27)) == date(2026, 1, 23)


def test_session_states():
    d = datetime(2026, 8, 4, tzinfo=IST)  # a Tuesday
    assert cal.session_state(d.replace(hour=8, minute=0)) == SessionState.CLOSED
    assert cal.session_state(d.replace(hour=9, minute=5)) == SessionState.PRE_OPEN
    assert cal.session_state(d.replace(hour=9, minute=15)) == SessionState.OPEN
    assert cal.session_state(d.replace(hour=15, minute=29)) == SessionState.OPEN
    assert cal.session_state(d.replace(hour=15, minute=45)) == SessionState.POST_CLOSE
    assert cal.session_state(d.replace(hour=16, minute=30)) == SessionState.CLOSED


def test_holiday_is_closed_all_day():
    d = datetime(2026, 1, 26, 10, 0, tzinfo=IST)
    assert cal.session_state(d) == SessionState.CLOSED


def test_muhurat_known():
    assert cal.muhurat_date(2025) == date(2025, 10, 21)


def test_refuses_empty_holiday_update():
    import pytest

    with pytest.raises(ValueError):
        MarketCalendar().update_holidays({})
