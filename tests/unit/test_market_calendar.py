from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.clock.market_calendar import CalendarCoverageError, MarketPhase, UsEquityCalendar


@pytest.mark.parametrize(
    "year,days",
    [
        (
            2026,
            "01-01 01-19 02-16 04-03 05-25 06-19 07-03 09-07 11-26 12-25",
        ),
        (
            2027,
            "01-01 01-18 02-15 03-26 05-31 06-18 07-05 09-06 11-25 12-24",
        ),
        (2028, "01-17 02-21 04-14 05-29 06-19 07-04 09-04 11-23 12-25"),
    ],
)
def test_holidays_match_entire_official_nyse_2026_2028_schedule(year: int, days: str) -> None:
    calendar = UsEquityCalendar()
    expected = frozenset(date.fromisoformat(f"{year}-{day}") for day in days.split())
    assert calendar.holidays(year) == expected
    for day in expected:
        assert calendar.regular_session_close(day) is None
        assert calendar.phase(datetime.combine(day, datetime.min.time(), calendar.timezone)) is (
            MarketPhase.HOLIDAY
        )


@pytest.mark.parametrize("year", [2021, 2027])
def test_saturday_new_year_does_not_close_previous_friday(year: int) -> None:
    calendar = UsEquityCalendar()
    preceding_friday = date(year, 12, 31)
    assert preceding_friday.weekday() == 4
    assert preceding_friday not in calendar.holidays(year)
    assert calendar.is_regular_session(preceding_friday)


def test_juneteenth_observance_starts_in_2022() -> None:
    calendar = UsEquityCalendar()
    assert calendar.is_regular_session(date(2021, 6, 18))
    assert calendar.is_regular_session(date(2020, 6, 19))
    assert not calendar.is_regular_session(date(2022, 6, 20))
    assert date(2023, 1, 2) in calendar.holidays(2023)


@pytest.mark.parametrize(
    "year,days",
    [
        (2026, "11-27 12-24"),
        (2027, "11-26"),
        (2028, "07-03 11-24"),
    ],
)
def test_exact_early_close_dates_and_phase_boundaries(year: int, days: str) -> None:
    calendar = UsEquityCalendar()
    expected = frozenset(date.fromisoformat(f"{year}-{day}") for day in days.split())
    assert calendar.early_closes(year) == expected
    for day in expected:
        close = calendar.regular_session_close(day)
        assert close == datetime(day.year, day.month, day.day, 13, tzinfo=calendar.timezone)
        assert calendar.phase(close - timedelta(microseconds=1)) is MarketPhase.REGULAR
        assert calendar.phase(close) is MarketPhase.POST_MARKET
        assert calendar.phase(close.astimezone(UTC)) is MarketPhase.POST_MARKET
        assert calendar.phase(close.replace(hour=16, minute=59)) is MarketPhase.POST_MARKET
        assert calendar.phase(close.replace(hour=17)) is MarketPhase.OVERNIGHT


@pytest.mark.parametrize("day", [date(2026, 7, 2), date(2027, 7, 2), date(2027, 12, 31)])
def test_eves_without_published_early_closes_remain_full_sessions(day: date) -> None:
    calendar = UsEquityCalendar()
    close = calendar.regular_session_close(day)
    assert close is not None and close.hour == 16
    assert calendar.phase(close.replace(hour=13)) is MarketPhase.REGULAR
    assert calendar.phase(close) is MarketPhase.POST_MARKET
    assert calendar.phase(close.replace(hour=20)) is MarketPhase.OVERNIGHT


@pytest.mark.parametrize(
    "day,utc_hour",
    [(date(2026, 3, 6), 21), (date(2026, 3, 9), 20), (date(2026, 11, 2), 21)],
)
def test_close_api_preserves_new_york_dst(day: date, utc_hour: int) -> None:
    close = UsEquityCalendar().regular_session_close(day)
    assert close is not None
    assert close.hour == 16
    assert close.astimezone(UTC).hour == utc_hour


@pytest.mark.parametrize("year", [2025, 2029])
def test_unverified_schedule_never_silently_assumes_a_regular_close(year: int) -> None:
    calendar = UsEquityCalendar()
    with pytest.raises(CalendarCoverageError, match="unverified"):
        calendar.regular_session_close(date(year, 1, 2))
    with pytest.raises(CalendarCoverageError, match="unverified"):
        calendar.early_closes(year)
    with pytest.raises(CalendarCoverageError, match="unverified"):
        calendar.phase(datetime(year, 1, 2, 15, tzinfo=UTC))


def test_weekend_and_naive_timestamp_guards() -> None:
    calendar = UsEquityCalendar()
    assert calendar.regular_session_close(date(2028, 1, 1)) is None
    assert calendar.phase(datetime(2028, 1, 1, 15, tzinfo=UTC)) is MarketPhase.WEEKEND
    with pytest.raises(ValueError, match="timezone-aware"):
        calendar.phase(datetime(2026, 9, 3, 15))
