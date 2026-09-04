from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.api.dashboard import RuntimeView
from app.clock.base import Clock, VirtualClock
from app.config import AppConfig, RuntimeBinding
from app.controller import master as master_module
from app.controller.master import InstanceLock, MasterController, PeriodicJob
from app.domain.enums import (
    DemoBackend,
    ExecutionEnvironment,
    RuntimeSafetyState,
    TradingMode,
)
from app.observability.metrics import MetricsRegistry
from app.safety.runtime_state import SafetyController, SafetyEvidence


async def _true() -> bool:
    return True


def _app_config(
    tmp_path: Path, *, environment_execution_disabled: bool = False
) -> AppConfig:
    return AppConfig.model_validate(
        {
            "version": "controller-tests",
            "execution_environment": "DEMO",
            "demo_backend": "OFFLINE_SIM",
            "trading_mode": "RESEARCH",
            "database": {"url": "postgresql+asyncpg://unused"},
            "runtime": {
                "instance_lock_dir": tmp_path / "locks",
                "disabled_file": tmp_path / "TRADING_DISABLED",
                "environment_execution_disabled": environment_execution_disabled,
                "startup_health_window_seconds": 0,
            },
            "dashboard": {},
            "sentinel": {},
        }
    )


def _binding(tmp_path: Path) -> RuntimeBinding:
    return RuntimeBinding(
        environment=ExecutionEnvironment.DEMO,
        demo_backend=DemoBackend.OFFLINE_SIM,
        database_schema="demo",
        runtime_directory=tmp_path,
        idempotency_namespace="controller-tests",
        external_write_authority=False,
        config_version="controller-tests",
    )


def _healthy_evidence() -> SafetyEvidence:
    return SafetyEvidence(True, True, True, True, True, True, True, True)


def _normal_view(binding: RuntimeBinding, clock: Clock) -> RuntimeView:
    safety = SafetyController(clock, timedelta(0))
    safety.observe(_healthy_evidence())
    safety.observe(_healthy_evidence())
    assert safety.state is RuntimeSafetyState.NORMAL
    return RuntimeView(
        binding=binding,
        trading_mode=TradingMode.RESEARCH,
        safety=safety,
        broker_connected=True,
        database_healthy=True,
        market_data_fresh=True,
        reconciled=True,
        execution_service_healthy=True,
        unresolved_submission=False,
    )


def _controller(
    tmp_path: Path,
    clock: Clock,
    *,
    repository_health: Callable[[], Awaitable[bool]] = _true,
    broker_health: Callable[[], Awaitable[bool]] = _true,
    reconcile: Callable[[], Awaitable[bool]] = _true,
    execution_health: Callable[[], Awaitable[bool]] | None = _true,
    environment_execution_disabled: bool = False,
    health_timeout_seconds: float = 10.0,
) -> tuple[MasterController, RuntimeView, MetricsRegistry]:
    view = _normal_view(_binding(tmp_path), clock)
    metrics = MetricsRegistry()
    controller = MasterController(
        _app_config(
            tmp_path,
            environment_execution_disabled=environment_execution_disabled,
        ),
        view,
        clock,
        metrics,
        repository_health=repository_health,
        broker_health=broker_health,
        reconcile=reconcile,
        execution_health=execution_health,
        health_timeout_seconds=health_timeout_seconds,
    )
    return controller, view, metrics


def test_instance_lock_is_atomic_and_only_owner_releases(tmp_path: Path) -> None:
    path = tmp_path / "locks" / "demo.lock"
    owner = InstanceLock(path)
    contender = InstanceLock(path)

    owner.acquire()
    assert path.exists()

    with pytest.raises(RuntimeError, match="runtime lock already exists"):
        contender.acquire()
    contender.release()
    assert path.exists()

    owner.release()
    with InstanceLock(path):
        pass


def test_instance_lock_context_releases_after_failure(tmp_path: Path) -> None:
    path = tmp_path / "demo.lock"

    with pytest.raises(LookupError):
        with InstanceLock(path):
            assert path.exists()
            raise LookupError("startup failed")

    with InstanceLock(path):
        pass


@pytest.mark.parametrize("stale_contents", ["999999", ""])
def test_instance_lock_can_recover_unlocked_stale_file(
    tmp_path: Path, stale_contents: str
) -> None:
    path = tmp_path / "demo.lock"
    path.write_text(stale_contents, encoding="ascii")

    with InstanceLock(path):
        contender = InstanceLock(path)
        with pytest.raises(RuntimeError, match="runtime lock already exists"):
            contender.acquire()

    with InstanceLock(path):
        pass


def test_instance_lock_release_cannot_remove_successor_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "demo.lock"
    owner = InstanceLock(path)
    successor = InstanceLock(path)
    contender = InstanceLock(path)
    original_unlock = master_module._unlock_descriptor

    owner.acquire()

    def unlock_then_handoff(descriptor: int) -> None:
        original_unlock(descriptor)
        successor.acquire()

    monkeypatch.setattr(master_module, "_unlock_descriptor", unlock_then_handoff)
    try:
        owner.release()
        with pytest.raises(RuntimeError, match="runtime lock already exists"):
            contender.acquire()
    finally:
        monkeypatch.setattr(master_module, "_unlock_descriptor", original_unlock)
        successor.release()


def test_controller_rejects_duplicate_job_names(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, _, _ = _controller(tmp_path, clock)

    async def callback() -> None:
        return None

    controller.add_job(PeriodicJob("positions", 60, callback))
    with pytest.raises(ValueError, match="duplicate controller job"):
        controller.add_job(PeriodicJob("positions", 30, callback))


@pytest.mark.parametrize(
    ("name", "interval", "message"),
    [
        ("", 1, "name cannot be empty"),
        ("   ", 1, "name cannot be empty"),
        ("positions", 0, "interval must be positive"),
        ("positions", -1, "interval must be positive"),
    ],
)
def test_periodic_job_rejects_invalid_schedule(
    name: str, interval: int, message: str
) -> None:
    async def callback() -> None:
        return None

    with pytest.raises(ValueError, match=message):
        PeriodicJob(name, interval, callback)


def test_controller_rejects_collision_with_builtin_health_job(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, _, _ = _controller(tmp_path, clock)

    async def callback() -> None:
        return None

    with pytest.raises(ValueError, match="duplicate controller job"):
        controller.add_job(PeriodicJob("health", 30, callback))


@pytest.mark.asyncio
async def test_failed_reconciliation_disables_entries(
    tmp_path: Path, clock: VirtualClock
) -> None:
    async def reconciliation_failed() -> bool:
        return False

    controller, view, _ = _controller(tmp_path, clock, reconcile=reconciliation_failed)

    assert not await controller.reconcile()
    assert not view.reconciled
    assert view.safety.state is RuntimeSafetyState.ENTRY_DISABLED
    assert view.safety.reason == "reconciliation failed"


@pytest.mark.asyncio
async def test_reconciliation_exception_cannot_leave_entries_enabled(
    tmp_path: Path, clock: VirtualClock
) -> None:
    async def reconciliation_raised() -> bool:
        raise ConnectionError("broker unavailable")

    controller, view, _ = _controller(tmp_path, clock, reconcile=reconciliation_raised)

    try:
        await controller.reconcile()
    except ConnectionError:
        pass

    assert not view.reconciled
    assert not view.safety.permits_new_entry()


@pytest.mark.asyncio
async def test_database_health_failure_halts_and_records_evidence(
    tmp_path: Path, clock: VirtualClock
) -> None:
    async def unhealthy() -> bool:
        return False

    controller, view, metrics = _controller(tmp_path, clock, repository_health=unhealthy)

    await controller.health_once()

    assert view.safety.state is RuntimeSafetyState.HALTED
    assert not view.database_healthy
    assert view.broker_connected
    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.database_writable
    rendered = await metrics.render_prometheus()
    assert "options_sentinel_runtime_safety_state 3" in rendered


@pytest.mark.asyncio
async def test_healthcheck_exception_cannot_leave_entries_enabled(
    tmp_path: Path, clock: VirtualClock
) -> None:
    async def health_raised() -> bool:
        raise ConnectionError("database unavailable")

    controller, view, _ = _controller(tmp_path, clock, repository_health=health_raised)

    try:
        await controller.health_once()
    except ConnectionError:
        pass

    assert not view.database_healthy
    assert view.safety.state is RuntimeSafetyState.HALTED
    assert not view.safety.permits_new_entry()


@pytest.mark.asyncio
async def test_kill_switch_file_disables_entries_and_is_in_evidence(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, view, _ = _controller(tmp_path, clock)
    controller.config.runtime.disabled_file.touch()

    await controller.health_once()

    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.kill_switch_clear
    assert view.safety.state is RuntimeSafetyState.HALTED
    assert not view.safety.permits_new_entry()


@pytest.mark.asyncio
async def test_missing_execution_health_evidence_fails_closed(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, view, _ = _controller(tmp_path, clock, execution_health=None)
    view.execution_service_healthy = False

    await controller.health_once()

    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.execution_service_healthy
    assert view.safety.state is RuntimeSafetyState.ENTRY_DISABLED


@pytest.mark.asyncio
async def test_execution_health_exception_fails_closed(
    tmp_path: Path, clock: VirtualClock
) -> None:
    async def execution_raised() -> bool:
        raise ConnectionError("sensitive upstream response")

    controller, view, _ = _controller(tmp_path, clock, execution_health=execution_raised)

    await controller.health_once()

    assert not view.execution_service_healthy
    assert view.safety.state is RuntimeSafetyState.ENTRY_DISABLED
    assert "execution-health: ConnectionError" in view.recent_errors
    assert all("sensitive" not in error for error in view.recent_errors)


@pytest.mark.asyncio
async def test_unresolved_submission_evidence_halts_controller(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, view, _ = _controller(tmp_path, clock)
    view.unresolved_submission = True

    await controller.health_once()

    assert view.last_safety_evidence is not None
    assert view.last_safety_evidence.unresolved_submission
    assert view.safety.state is RuntimeSafetyState.HALTED


@pytest.mark.asyncio
async def test_runtime_binding_mismatch_disables_entries(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, view, _ = _controller(tmp_path, clock)
    view.binding = view.binding.model_copy(update={"demo_backend": DemoBackend.BROKER_SHADOW})

    await controller.health_once()

    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.environment_matches
    assert view.safety.state is RuntimeSafetyState.ENTRY_DISABLED


@pytest.mark.asyncio
async def test_configured_execution_kill_switch_halts_runtime(
    tmp_path: Path, clock: VirtualClock
) -> None:
    controller, view, _ = _controller(
        tmp_path,
        clock,
        environment_execution_disabled=True,
    )

    await controller.health_once()

    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.kill_switch_clear
    assert view.safety.state is RuntimeSafetyState.HALTED
    assert not view.safety.permits_new_entry()


class _BlockingClock(Clock):
    def __init__(self, now: datetime) -> None:
        self._now = now
        self.sleep_started = asyncio.Event()
        self.release_sleep = asyncio.Event()
        self.last_sleep_seconds: float | None = None

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.last_sleep_seconds = seconds
        self.sleep_started.set()
        await self.release_sleep.wait()


@pytest.mark.asyncio
async def test_unexpected_startup_failure_releases_instance_lock(
    tmp_path: Path, instant: datetime, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _BlockingClock(instant)
    controller, _, _ = _controller(tmp_path, clock)
    lock_path = controller.config.runtime.instance_lock_dir / "demo.lock"

    async def startup_health_raised() -> None:
        raise OSError("metrics unavailable")

    monkeypatch.setattr(controller, "health_once", startup_health_raised)

    with pytest.raises(OSError, match="metrics unavailable"):
        await controller.start()

    with InstanceLock(lock_path):
        pass


@pytest.mark.asyncio
async def test_failed_startup_does_not_strand_instance_lock(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)

    async def reconciliation_raised() -> bool:
        raise ConnectionError("broker unavailable")

    controller, view, _ = _controller(tmp_path, clock, reconcile=reconciliation_raised)
    lock_path = controller.config.runtime.instance_lock_dir / "demo.lock"

    try:
        await controller.start()
    except ConnectionError:
        with InstanceLock(lock_path):
            pass
    else:
        try:
            assert not view.safety.permits_new_entry()
        finally:
            await controller.stop()


@pytest.mark.asyncio
async def test_successful_periodic_job_records_metric(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)
    controller, view, metrics = _controller(tmp_path, clock)

    async def successful_job() -> None:
        return None

    task = asyncio.create_task(
        controller._job_loop(PeriodicJob("positions", 11, successful_job))
    )
    await asyncio.wait_for(clock.sleep_started.wait(), timeout=1)
    try:
        assert view.safety.state is RuntimeSafetyState.NORMAL
        rendered = await metrics.render_prometheus()
        assert "options_sentinel_job_positions_success_total 1" in rendered
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_periodic_job_cancellation_is_not_recorded_as_failure(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)
    controller, view, metrics = _controller(tmp_path, clock)
    callback_started = asyncio.Event()
    remain_blocked = asyncio.Event()

    async def cancelled_job() -> None:
        callback_started.set()
        await remain_blocked.wait()

    task = asyncio.create_task(
        controller._job_loop(PeriodicJob("positions", 11, cancelled_job))
    )
    await asyncio.wait_for(callback_started.wait(), timeout=1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert view.safety.state is RuntimeSafetyState.NORMAL
    assert not view.recent_errors
    rendered = await metrics.render_prometheus()
    assert "job_positions_failure_total" not in rendered


@pytest.mark.asyncio
async def test_periodic_job_failure_is_contained_and_disables_entries(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)
    controller, view, metrics = _controller(tmp_path, clock)

    async def failed_job() -> None:
        raise RuntimeError("feed disconnected")

    job = PeriodicJob("catalysts", 17, failed_job)
    task = asyncio.create_task(controller._job_loop(job))
    await asyncio.wait_for(clock.sleep_started.wait(), timeout=1)
    try:
        assert not task.done()
        assert clock.last_sleep_seconds == 17
        assert view.safety.state is RuntimeSafetyState.ENTRY_DISABLED
        assert view.recent_errors[-1] == "catalysts: RuntimeError"
        rendered = await metrics.render_prometheus()
        assert "options_sentinel_job_catalysts_failure_total 1" in rendered
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 3601])
def test_periodic_job_rejects_unbounded_deadline(timeout: float) -> None:
    async def callback() -> None:
        return None

    with pytest.raises(ValueError, match="timeout must be finite"):
        PeriodicJob("positions", 60, callback, timeout_seconds=timeout)


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 11])
def test_controller_rejects_unbounded_health_deadline(
    tmp_path: Path, clock: VirtualClock, timeout: float
) -> None:
    with pytest.raises(ValueError, match="health timeout must be finite"):
        _controller(tmp_path, clock, health_timeout_seconds=timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("component", ["database", "broker", "execution"])
async def test_health_timeout_is_bounded_latched_and_never_replayed(
    tmp_path: Path, clock: VirtualClock, component: str
) -> None:
    calls = 0
    cancelled = asyncio.Event()

    async def blocked() -> bool:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    controller, view, _ = _controller(
        tmp_path,
        clock,
        repository_health=blocked if component == "database" else _true,
        broker_health=blocked if component == "broker" else _true,
        execution_health=blocked if component == "execution" else _true,
        health_timeout_seconds=0.02,
    )
    await asyncio.wait_for(controller.health_once(), timeout=0.3)
    await asyncio.wait_for(cancelled.wait(), timeout=0.3)
    assert view.safety.state is RuntimeSafetyState.HALTED
    assert not view.reconciled
    assert not view.execution_service_healthy
    assert view.last_safety_evidence is not None
    assert not view.last_safety_evidence.permits_normal
    assert f"{component}-health: TimeoutError" in view.recent_errors
    await controller.health_once()
    assert calls == 1
    assert not await controller.reconcile()
    await controller.stop()


@pytest.mark.asyncio
async def test_health_timeout_invalidates_evidence_before_other_probes_finish(
    tmp_path: Path, clock: VirtualClock
) -> None:
    release_broker = asyncio.Event()
    broker_started = asyncio.Event()

    async def database_timeout() -> bool:
        raise TimeoutError("secret connection details must not be recorded")

    async def broker_wait() -> bool:
        broker_started.set()
        await release_broker.wait()
        return True

    controller, view, _ = _controller(
        tmp_path, clock, repository_health=database_timeout, broker_health=broker_wait,
        health_timeout_seconds=0.2,
    )
    task = asyncio.create_task(controller.health_once())
    await broker_started.wait()
    await asyncio.sleep(0.01)
    try:
        assert not task.done()
        assert view.safety.state is RuntimeSafetyState.HALTED
        assert not view.database_healthy
        assert view.last_safety_evidence is not None
        assert not view.last_safety_evidence.database_writable
        assert all("secret" not in item for item in view.recent_errors)
    finally:
        release_broker.set()
        await task
        await controller.stop()


@pytest.mark.asyncio
async def test_timed_out_job_stops_without_retry_and_independent_monitor_keeps_running(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)
    controller, view, metrics = _controller(tmp_path, clock, health_timeout_seconds=0.02)
    calls = 0
    monitored = asyncio.Event()

    async def interrupted_dispatch() -> None:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    async def positions() -> None:
        monitored.set()

    dispatch = asyncio.create_task(controller._job_loop(
        PeriodicJob("dispatch", 1, interrupted_dispatch, timeout_seconds=0.02)
    ))
    monitor = asyncio.create_task(controller._job_loop(PeriodicJob("positions", 1, positions)))
    try:
        await asyncio.wait_for(dispatch, timeout=0.3)
        await asyncio.wait_for(monitored.wait(), timeout=0.3)
        assert not monitor.done()
        assert calls == 1
        assert view.safety.state is RuntimeSafetyState.HALTED
        assert "dispatch: TimeoutError" in view.recent_errors
        assert "options_sentinel_job_dispatch_failure_total 1" in await metrics.render_prometheus()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        await controller.stop()


async def _ignore_cancellation_until(release: asyncio.Event) -> None:
    while not release.is_set():
        try:
            await release.wait()
        except asyncio.CancelledError:
            continue


@pytest.mark.asyncio
async def test_resistant_job_retains_lock_until_callback_actually_finishes(
    tmp_path: Path, instant: datetime
) -> None:
    clock = _BlockingClock(instant)
    controller, view, _ = _controller(tmp_path, clock, health_timeout_seconds=0.02)
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    async def resistant_dispatch() -> None:
        nonlocal calls
        calls += 1
        await _ignore_cancellation_until(release)
        # Simulate late mutation by a callback that incorrectly suppressed cancellation.
        view.reconciled = True
        view.execution_service_healthy = True
        completed.set()

    controller.add_job(PeriodicJob("dispatch", 1, resistant_dispatch, timeout_seconds=0.02))
    await controller.start()
    contender = InstanceLock(controller.config.runtime.instance_lock_dir / "demo.lock")
    try:
        await asyncio.sleep(0.05)
        assert view.safety.state is RuntimeSafetyState.HALTED
        with pytest.raises(RuntimeError, match="shutdown incomplete"):
            await controller.stop()
        with pytest.raises(RuntimeError, match="runtime lock already exists"):
            contender.acquire()
        assert calls == 1
        assert not completed.is_set()
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=0.3)
        await asyncio.sleep(0)
        assert not view.reconciled
        assert not view.execution_service_healthy
        assert view.safety.state is RuntimeSafetyState.HALTED
    finally:
        release.set()
        await completed.wait()
        await asyncio.sleep(0)
        await controller.stop()
    with contender:
        pass


@pytest.mark.asyncio
async def test_late_health_success_is_discarded_and_cannot_clear_timeout_latch(
    tmp_path: Path, clock: VirtualClock
) -> None:
    release = asyncio.Event()
    completed = asyncio.Event()

    async def resistant_health() -> bool:
        await _ignore_cancellation_until(release)
        view.database_healthy = True
        view.reconciled = True
        completed.set()
        return True

    controller, view, _ = _controller(
        tmp_path, clock, repository_health=resistant_health, health_timeout_seconds=0.02
    )
    try:
        await asyncio.wait_for(controller.health_once(), timeout=0.3)
        assert not view.database_healthy
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=0.3)
        await asyncio.sleep(0)
        assert not view.database_healthy
        assert not view.reconciled
        assert not view.execution_service_healthy
        assert view.safety.state is RuntimeSafetyState.HALTED
        assert view.last_safety_evidence is not None
        assert not view.last_safety_evidence.permits_normal
    finally:
        release.set()
        await completed.wait()
        await asyncio.sleep(0)
        await controller.stop()


@pytest.mark.asyncio
async def test_external_health_cancellation_propagates_without_timeout_classification(
    tmp_path: Path, clock: VirtualClock
) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked() -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        return True

    controller, view, _ = _controller(tmp_path, clock, repository_health=blocked)
    task = asyncio.create_task(controller.health_once())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(cancelled.wait(), timeout=0.3)
    assert not controller._timed_out_callbacks
    assert not view.recent_errors
    await controller.stop()


@pytest.mark.asyncio
async def test_interrupted_startup_keeps_lock_while_reconciliation_still_runs(
    tmp_path: Path, clock: VirtualClock
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def resistant_reconcile() -> bool:
        entered.set()
        await _ignore_cancellation_until(release)
        completed.set()
        return True

    controller, view, _ = _controller(
        tmp_path, clock, reconcile=resistant_reconcile, health_timeout_seconds=0.02
    )
    startup = asyncio.create_task(controller.start())
    await entered.wait()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup
    contender = InstanceLock(controller.config.runtime.instance_lock_dir / "demo.lock")
    try:
        with pytest.raises(RuntimeError, match="runtime lock already exists"):
            contender.acquire()
        assert view.safety.state is RuntimeSafetyState.HALTED
    finally:
        release.set()
        await completed.wait()
        await asyncio.sleep(0)
        await controller.stop()
    with contender:
        pass


@pytest.mark.asyncio
async def test_controller_start_cannot_duplicate_existing_job_loops(
    tmp_path: Path, instant: datetime
) -> None:
    controller, _, _ = _controller(tmp_path, _BlockingClock(instant))
    await controller.start()
    try:
        with pytest.raises(RuntimeError, match="cannot be started twice"):
            await controller.start()
    finally:
        await controller.stop()


@pytest.mark.asyncio
async def test_concurrent_reconciliation_does_not_overlap_or_accept_late_success(
    tmp_path: Path, clock: VirtualClock
) -> None:
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    async def resistant_reconcile() -> bool:
        nonlocal calls
        calls += 1
        await _ignore_cancellation_until(release)
        view.reconciled = True
        completed.set()
        return True

    controller, view, _ = _controller(
        tmp_path, clock, reconcile=resistant_reconcile, health_timeout_seconds=0.02
    )
    try:
        results = await asyncio.wait_for(
            asyncio.gather(controller.reconcile(), controller.reconcile()), timeout=0.3
        )
        assert results == [False, False]
        assert calls == 1
        assert view.safety.state is RuntimeSafetyState.HALTED
        release.set()
        await asyncio.wait_for(completed.wait(), timeout=0.3)
        await asyncio.sleep(0)
        assert not view.reconciled
        assert not await controller.reconcile()
        assert calls == 1
    finally:
        release.set()
        await completed.wait()
        await asyncio.sleep(0)
        await controller.stop()
