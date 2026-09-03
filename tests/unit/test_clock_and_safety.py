from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.clock.base import VirtualClock
from app.clock.market_calendar import MarketPhase, UsEquityCalendar
from app.domain.enums import RuntimeSafetyState
from app.safety.runtime_state import SafetyController, SafetyEvidence


@pytest.mark.asyncio
async def test_virtual_clock_never_moves_backward(clock: VirtualClock) -> None:
    initial = clock.now()
    await clock.advance(timedelta(seconds=5))
    assert clock.now() == initial + timedelta(seconds=5)
    with pytest.raises(ValueError, match="backward"):
        await clock.advance_to(initial)


def test_market_calendar_handles_holiday_and_regular_session() -> None:
    calendar = UsEquityCalendar()
    assert not calendar.is_regular_session(date(2026, 9, 7))  # Labor Day
    assert calendar.is_regular_session(date(2026, 9, 8))
    regular = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert calendar.phase(regular) is MarketPhase.REGULAR


@pytest.mark.asyncio
async def test_safety_requires_continuous_health_window(clock: VirtualClock) -> None:
    controller = SafetyController(clock, timedelta(seconds=30))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    await clock.advance(timedelta(seconds=29))
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    await clock.advance(timedelta(seconds=1))
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.NORMAL


def test_unresolved_submission_halts(clock: VirtualClock) -> None:
    controller = SafetyController(clock, timedelta(0))
    evidence = SafetyEvidence(
        True, True, True, True, True, True, True, True, unresolved_submission=True
    )
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.HALTED
    assert not controller.permits_new_entry()
