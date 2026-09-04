from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from app.clock import base as clock_module
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


def test_real_health_window_uses_monotonic_elapsed_time(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = [100.0]
    monkeypatch.setattr(clock_module.time, "monotonic", lambda: elapsed[0])
    clock = clock_module.RealClock()
    controller = SafetyController(clock, timedelta(seconds=30))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    controller.observe(evidence)
    # Wall-clock retrieval is irrelevant to elapsed-time qualification.
    monkeypatch.setattr(clock, "now", lambda: datetime(2099, 1, 1, tzinfo=UTC))
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    elapsed[0] += 30
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.NORMAL


def test_backward_elapsed_clock_restarts_health_window(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = [100.0]
    clock = clock_module.RealClock()
    monkeypatch.setattr(clock, "elapsed_seconds", lambda: elapsed[0])
    controller = SafetyController(clock, timedelta(seconds=30))
    evidence = SafetyEvidence(True, True, True, True, True, True, True, True)
    controller.observe(evidence)
    elapsed[0] = 10
    controller.observe(evidence)
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    assert controller.reason == "elapsed clock moved backward"
    elapsed[0] = 100
    controller.observe(evidence)
    assert controller.reason == "health window accumulating"
