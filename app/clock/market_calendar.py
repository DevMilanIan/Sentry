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


class CalendarCoverageError(ValueError):
    """The published session schedule has not been verified for this year."""


_VERIFIED_EARLY_CLOSES = {
    2026: frozenset({date(2026, 11, 27), date(2026, 12, 24)}),
    2027: frozenset({date(2027, 11, 26)}),
    2028: frozenset({date(2028, 7, 3), date(2028, 11, 24)}),
}


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
    """NYSE scheduled equity sessions, verified for 2026 through 2028 only.

    Sources: https://www.nyse.com/trade/hours-calendars and NYSE Group's
    December 23, 2025 holiday/early-close announcement (verified 2026-09-03).
    Close/phase queries fail explicitly outside the published schedule. Nominal
    holiday rules remain available for historical date arithmetic but do not
    constitute a complete historical calendar. Unscheduled closures, exchange
    halts, broker availability, and product-specific options hours require
    independent provider evidence; this calendar never authorizes execution.
    """

    timezone = ZoneInfo("America/New_York")
    verified_years = frozenset(_VERIFIED_EARLY_CLOSES)
    schedule_version = "nyse-scheduled-2026-2028-verified-2026-09-03"

    def _require_verified_year(self, year: int) -> None:
        if year not in self.verified_years:
            raise CalendarCoverageError(
                f"NYSE session schedule is unverified for {year}; "
                "refresh the official holiday and early-close calendar"
            )

    def holidays(self, year: int) -> frozenset[date]:
        """Nominal annual holidays, without historical unscheduled closures."""
        days = {
            _nth_weekday(year, 1, 0, 3),  # MLK
            _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
            _easter_sunday(year) - timedelta(days=2),
            _last_weekday(year, 5, 0),
            _observed(date(year, 7, 4)),
            _nth_weekday(year, 9, 0, 1),
            _nth_weekday(year, 11, 3, 4),
            _observed(date(year, 12, 25)),
        }
        # NYSE does not observe Saturday New Year's Day on the previous Friday.
        # The official 2028 schedule explicitly leaves 2027-12-31 open.
        new_year = date(year, 1, 1)
        if new_year.weekday() != 5:
            days.add(_observed(new_year))
        # NYSE added this exchange holiday for 2022, not the 2021 federal date.
        if year >= 2022:
            days.add(_observed(date(year, 6, 19)))
        return frozenset(day for day in days if day.year == year)

    def is_regular_session(self, day: date) -> bool:
        """Whether nominal holiday rules allow a session; not live-open evidence."""
        return day.weekday() < 5 and day not in self.holidays(day.year)

    def early_closes(self, year: int) -> frozenset[date]:
        self._require_verified_year(year)
        return _VERIFIED_EARLY_CLOSES[year]

    def regular_session_close(self, day: date) -> datetime | None:
        """Scheduled equity close in New York time; None on a scheduled closed day.

        Eligible options may trade until 13:15 on early-close dates. This equity
        API intentionally does not infer a contract's options trading cutoff.
        """
        self._require_verified_year(day.year)
        if not self.is_regular_session(day):
            return None
        close = time(13) if day in self.early_closes(day.year) else time(16)
        return datetime.combine(day, close, tzinfo=self.timezone)

    def phase(self, timestamp: datetime) -> MarketPhase:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        local = timestamp.astimezone(self.timezone)
        self._require_verified_year(local.year)
        if local.weekday() >= 5:
            return MarketPhase.WEEKEND
        if local.date() in self.holidays(local.year):
            return MarketPhase.HOLIDAY
        value = local.timetz().replace(tzinfo=None)
        if value < time(4, 0):
            return MarketPhase.OVERNIGHT
        if value < time(9, 30):
            return MarketPhase.PRE_MARKET
        close = self.regular_session_close(local.date())
        assert close is not None
        if local < close:
            return MarketPhase.REGULAR
        # The listed NYSE late equity sessions end at 17:00 on early-close days.
        # These broad phase labels are not product-specific execution evidence.
        late_close = time(17) if local.date() in self.early_closes(local.year) else time(20)
        if value < late_close:
            return MarketPhase.POST_MARKET
        return MarketPhase.OVERNIGHT
