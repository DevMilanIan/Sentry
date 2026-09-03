from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.clock.base import VirtualClock
from app.domain.enums import RuntimeSafetyState
from app.safety.runtime_state import SafetyController, SafetyEvidence


def _healthy() -> SafetyEvidence:
    return SafetyEvidence(True, True, True, True, True, True, True, True)


def _normal_controller(clock: VirtualClock) -> SafetyController:
    controller = SafetyController(clock, timedelta(0))
    controller.observe(_healthy())
    controller.observe(_healthy())
    assert controller.state is RuntimeSafetyState.NORMAL
    return controller


def test_degrade_cannot_be_used_to_request_normal(clock: VirtualClock) -> None:
    controller = SafetyController(clock, timedelta(0))

    with pytest.raises(ValueError, match="cannot request NORMAL"):
        controller.degrade(RuntimeSafetyState.NORMAL, "unsafe bypass")

    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED


def test_lower_severity_degrade_does_not_overwrite_halt_reason(
    clock: VirtualClock,
) -> None:
    controller = SafetyController(clock, timedelta(0))
    controller.emergency_stop("operator kill switch")

    controller.degrade(RuntimeSafetyState.ENTRY_DISABLED, "background job failed")

    assert controller.state is RuntimeSafetyState.HALTED
    assert controller.reason == "operator kill switch"


@pytest.mark.parametrize(
    ("field", "expected_state"),
    [
        ("database_writable", RuntimeSafetyState.HALTED),
        ("broker_state_known", RuntimeSafetyState.ENTRY_DISABLED),
        ("reconciled", RuntimeSafetyState.ENTRY_DISABLED),
        ("market_data_fresh", RuntimeSafetyState.ENTRY_DISABLED),
        ("account_data_fresh", RuntimeSafetyState.ENTRY_DISABLED),
        ("execution_service_healthy", RuntimeSafetyState.ENTRY_DISABLED),
        ("kill_switch_clear", RuntimeSafetyState.HALTED),
        ("environment_matches", RuntimeSafetyState.ENTRY_DISABLED),
        ("unresolved_submission", RuntimeSafetyState.HALTED),
    ],
)
def test_every_missing_health_gate_fails_closed(
    clock: VirtualClock, field: str, expected_state: RuntimeSafetyState
) -> None:
    controller = _normal_controller(clock)
    value = True if field == "unresolved_submission" else False

    controller.observe(replace(_healthy(), **{field: value}))

    assert controller.state is expected_state
    assert not controller.permits_new_entry()


@pytest.mark.asyncio
async def test_failed_evidence_restarts_entire_health_window(clock: VirtualClock) -> None:
    controller = SafetyController(clock, timedelta(seconds=30))
    controller.observe(_healthy())
    await clock.advance(timedelta(seconds=29))
    controller.observe(replace(_healthy(), market_data_fresh=False))

    controller.observe(_healthy())
    await clock.advance(timedelta(seconds=29))
    controller.observe(_healthy())
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED

    await clock.advance(timedelta(seconds=1))
    controller.observe(_healthy())
    assert controller.permits_new_entry()


def test_emergency_stop_is_sticky_without_manual_clear(clock: VirtualClock) -> None:
    controller = _normal_controller(clock)
    controller.emergency_stop("operator kill switch")

    controller.observe(_healthy())

    assert controller.state is RuntimeSafetyState.HALTED
    assert controller.reason == "operator kill switch"
    assert not controller.permits_new_entry()
    assert not controller.permits_risk_reducing_exit()


@pytest.mark.asyncio
async def test_manual_halt_clear_still_requires_continuous_health_window(
    clock: VirtualClock,
) -> None:
    controller = SafetyController(clock, timedelta(seconds=10))
    controller.emergency_stop("operator kill switch")

    controller.observe(_healthy(), manual_halt_cleared=True)
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    await clock.advance(timedelta(seconds=9))
    controller.observe(_healthy())
    assert controller.state is RuntimeSafetyState.ENTRY_DISABLED
    await clock.advance(timedelta(seconds=1))
    controller.observe(_healthy())

    assert controller.permits_new_entry()


def test_manual_clear_cannot_override_active_kill_switch(clock: VirtualClock) -> None:
    controller = _normal_controller(clock)
    controller.emergency_stop("operator kill switch")

    controller.observe(
        replace(_healthy(), kill_switch_clear=False), manual_halt_cleared=True
    )

    assert controller.state is RuntimeSafetyState.HALTED
    assert not controller.permits_new_entry()


@pytest.mark.asyncio
async def test_entry_pause_is_latched_until_explicit_resume(clock: VirtualClock) -> None:
    controller = _normal_controller(clock)
    controller.pause_entries()

    await clock.advance(timedelta(hours=1))
    controller.observe(_healthy())
    controller.observe(_healthy())

    assert not controller.permits_new_entry()
    assert controller.permits_risk_reducing_exit()
    assert controller.reason == "entries paused by operator"

    controller.resume_entries(_healthy())
    controller.observe(_healthy())
    assert controller.permits_new_entry()


@pytest.mark.asyncio
async def test_explicit_resume_requires_new_continuous_health_window(
    clock: VirtualClock,
) -> None:
    controller = SafetyController(clock, timedelta(seconds=10))
    controller.pause_entries()
    await clock.advance(timedelta(hours=1))
    controller.resume_entries(_healthy())
    await clock.advance(timedelta(seconds=9))
    controller.observe(_healthy())
    assert not controller.permits_new_entry()

    await clock.advance(timedelta(seconds=1))
    controller.observe(_healthy())
    assert controller.permits_new_entry()


def test_explicit_resume_cannot_override_active_kill_switch(clock: VirtualClock) -> None:
    controller = _normal_controller(clock)
    controller.pause_entries()

    controller.resume_entries(
        replace(_healthy(), kill_switch_clear=False), manual_halt_cleared=True
    )

    assert controller.state is RuntimeSafetyState.HALTED
    assert not controller.permits_new_entry()
