from __future__ import annotations

from datetime import date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo


class MarketPhase(StrEnum):
    WEEKEND = "WEEKEND"
    HOLIDAY = "HOLIDAY"
    OVERNIGHT = "OVERNIGHT"
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    POST_MARKET = "POST_MARKET"


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, ordinal: int) -> date:
    current = date(year, month, 1)
    offset = (weekday - current.weekday()) % 7
    return current + timedelta(days=offset + 7 * (ordinal - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        current = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        current = date(year, month + 1, 1) - timedelta(days=1)
    return current - timedelta(days=(current.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian computus.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


class UsEquityCalendar:
    """Core US equity sessions; special unscheduled closures remain provider events."""

    timezone = ZoneInfo("America/New_York")

    def holidays(self, year: int) -> frozenset[date]:
        days = {
            _observed(date(year, 1, 1)),
            _nth_weekday(year, 1, 0, 3),  # MLK
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _easter_sunday(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 6, 19)),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
        # New Year's Day may be observed in the preceding year.
        days.add(_observed(date(year + 1, 1, 1)))
        return frozenset(day for day in days if day.year == year)

    def is_regular_session(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays(day.year)

    def phase(self, timestamp: datetime) -> MarketPhase:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = timestamp.astimezone(self.timezone)
        if local.weekday() >= 5:
            return MarketPhase.WEEKEND
        if local.date() in self.holidays(local.year):
            return MarketPhase.HOLIDAY
        value = local.timetz().replace(tzinfo=None)
        if value < time(4, 0):
            return MarketPhase.OVERNIGHT
        if value < time(9, 30):
            return MarketPhase.PRE_MARKET
        if value < time(16, 0):
            return MarketPhase.REGULAR
        if value < time(20, 0):
            return MarketPhase.POST_MARKET
        return MarketPhase.OVERNIGHT
