from __future__ import annotations

import asyncio
import importlib
import math
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.api.dashboard import RuntimeView
from app.clock.base import Clock
from app.config import AppConfig
from app.domain.enums import RuntimeSafetyState
from app.observability.metrics import MetricsRegistry
from app.safety.runtime_state import SafetyEvidence


@dataclass(frozen=True, slots=True)
class PeriodicJob:
    name: str
    interval_seconds: int
    callback: Callable[[], Awaitable[None]]
    # Candidate research already has an inner 180-second budget. The outer
    # default leaves room for durable checkpointing; cheap jobs may tighten it.
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("periodic job name cannot be empty")
        if self.interval_seconds <= 0:
            raise ValueError("periodic job interval must be positive")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 3600:
            raise ValueError("periodic job timeout must be finite and between 0 and 3600 seconds")


class InstanceLock:
    """OS-backed per-environment lock automatically released after process death."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._owned = False
        self._descriptor: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            _lock_descriptor(descriptor)
        except OSError as exc:
            os.close(descriptor)
            raise RuntimeError(f"runtime lock already exists: {self.path}") from exc
        os.ftruncate(descriptor, 0)
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.fsync(descriptor)
        self._descriptor = descriptor
        self._owned = True

    def release(self) -> None:
        if self._owned:
            assert self._descriptor is not None
            descriptor = self._descriptor
            self._descriptor = None
            self._owned = False
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    fcntl_module: Any = importlib.import_module("fcntl")
    fcntl_module.flock(
        descriptor,
        fcntl_module.LOCK_EX | fcntl_module.LOCK_NB,
    )


def _unlock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    fcntl_module: Any = importlib.import_module("fcntl")
    fcntl_module.flock(descriptor, fcntl_module.LOCK_UN)


class MasterController:
    """Deterministic asyncio supervisor. It never invokes model logic on a timer by default."""

    def __init__(
        self,
        config: AppConfig,
        view: RuntimeView,
        clock: Clock,
        metrics: MetricsRegistry,
        *,
        repository_health: Callable[[], Awaitable[bool]],
        broker_health: Callable[[], Awaitable[bool]],
        reconcile: Callable[[], Awaitable[bool]],
        execution_health: Callable[[], Awaitable[bool]] | None = None,
        health_timeout_seconds: float = 10.0,
    ) -> None:
        if not math.isfinite(health_timeout_seconds) or not 0 < health_timeout_seconds <= 10:
            raise ValueError("health timeout must be finite and between 0 and 10 seconds")
        self.config = config
        self.view = view
        self.clock = clock
        self.metrics = metrics
        self._repository_health = repository_health
        self._broker_health = broker_health
        self._reconcile = reconcile
        self._execution_health = execution_health
        self._health_timeout_seconds = health_timeout_seconds
        self._jobs: list[PeriodicJob] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._started = False
        self._health_lock = asyncio.Lock()
        self._reconcile_lock = asyncio.Lock()
        self._active_callbacks: dict[str, asyncio.Task[Any]] = {}
        self._timed_out_callbacks: set[str] = set()
        lock_name = f"{view.binding.environment.value.lower()}.lock"
        self._lock = InstanceLock(config.runtime.instance_lock_dir / lock_name)

    def add_job(self, job: PeriodicJob) -> None:
        if job.name == "health" or any(existing.name == job.name for existing in self._jobs):
            raise ValueError(f"duplicate controller job: {job.name}")
        self._jobs.append(job)

    async def reconcile(self) -> bool:
        async with self._reconcile_lock:
            return await self._reconcile_once()

    async def _reconcile_once(self) -> bool:
        self.view.reconciled = False
        if self._timed_out_callbacks:
            self._enforce_timeout_latch()
            return False
        try:
            result = await self._run_bounded(
                "reconciliation", self._reconcile, self._health_timeout_seconds
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._latch_timeout("reconciliation")
            self.view.recent_errors.append(
                f"reconciliation: {type(exc).__name__}"
            )
            self.view.safety.degrade(
                RuntimeSafetyState.ENTRY_DISABLED,
                "reconciliation raised an exception",
            )
            return False
        if self._timed_out_callbacks:
            self._enforce_timeout_latch()
            return False
        self.view.reconciled = result
        if not result:
            self.view.safety.degrade(RuntimeSafetyState.ENTRY_DISABLED, "reconciliation failed")
        return result

    async def health_once(self) -> None:
        async with self._health_lock:
            await self._health_once()

    async def _health_once(self) -> None:
        # Do not wait for a network/database probe before honoring the kill file.
        if not self._kill_switch_clear():
            self.view.safety.emergency_stop("kill switch active")
        database_healthy, broker_healthy = await asyncio.gather(
            self._safe_health_check("database", self._repository_health),
            self._safe_health_check("broker", self._broker_health),
        )
        execution_healthy = (
            await self._safe_health_check("execution", self._execution_health)
            if self._execution_health is not None
            else self.view.execution_service_healthy
        )
        if self._timed_out_callbacks:
            execution_healthy = False
            self._enforce_timeout_latch()
        self.view.execution_service_healthy = execution_healthy
        evidence = self._safety_evidence(database_healthy, broker_healthy, execution_healthy)
        self.view.database_healthy = database_healthy
        self.view.broker_connected = broker_healthy
        self.view.last_safety_evidence = evidence
        self.view.safety.observe(evidence)
        self._enforce_timeout_latch()
        await self.metrics.gauge(
            "runtime_safety_state", list(RuntimeSafetyState).index(self.view.safety.state)
        )

    def _kill_switch_clear(self) -> bool:
        return (
            not self.config.runtime.environment_execution_disabled
            and not self.config.runtime.disabled_file.exists()
        )

    def _safety_evidence(
        self, database_healthy: bool, broker_healthy: bool, execution_healthy: bool
    ) -> SafetyEvidence:
        return SafetyEvidence(
            database_writable=database_healthy,
            broker_state_known=broker_healthy,
            reconciled=self.view.reconciled,
            market_data_fresh=self.view.market_data_fresh,
            account_data_fresh=broker_healthy,
            execution_service_healthy=execution_healthy,
            kill_switch_clear=self._kill_switch_clear(),
            environment_matches=(
                self.config.execution_environment is self.view.binding.environment
                and self.config.demo_backend is self.view.binding.demo_backend
            ),
            unresolved_submission=self.view.unresolved_submission,
        )

    def _latch_timeout(self, key: str) -> None:
        self._timed_out_callbacks.add(key)
        self._enforce_timeout_latch()

    def _enforce_timeout_latch(self) -> None:
        if not self._timed_out_callbacks:
            return
        self.view.reconciled = False
        self.view.execution_service_healthy = False
        if "database-health" in self._timed_out_callbacks:
            self.view.database_healthy = False
        if "broker-health" in self._timed_out_callbacks:
            self.view.broker_connected = False
        self.view.last_safety_evidence = self._safety_evidence(
            self.view.database_healthy, self.view.broker_connected, False
        )
        self.view.safety.emergency_stop(
            "callback deadline exceeded; restart and reconciliation required"
        )

    async def _run_bounded[T](
        self, key: str, callback: Callable[[], Awaitable[T]], timeout_seconds: float
    ) -> T:
        if self._stop.is_set():
            raise asyncio.CancelledError
        prior = self._active_callbacks.get(key)
        if prior is not None and not prior.done():
            raise RuntimeError("callback is already running")
        async def invoke() -> T:
            return await callback()

        task: asyncio.Task[T] = asyncio.create_task(invoke(), name=f"sentinel-callback:{key}")
        self._active_callbacks[key] = task

        def completed(done: asyncio.Task[T]) -> None:
            if self._active_callbacks.get(key) is done:
                self._active_callbacks.pop(key, None)
            if not done.cancelled():
                # Late exceptions are consumed, never printed with upstream details.
                done.exception()
            # A callback that suppresses cancellation may mutate a shared view;
            # its late result never restores successful controller evidence.
            self._enforce_timeout_latch()

        task.add_done_callback(completed)
        try:
            finished, _ = await asyncio.wait({task}, timeout=timeout_seconds)
            if not finished:
                self._latch_timeout(key)
                task.cancel()
                # Do not await cancellation without a bound. The task remains
                # tracked, and shutdown cannot release the lock while it runs.
                raise TimeoutError("controller callback deadline exceeded")
            return task.result()
        except asyncio.CancelledError:
            task.cancel()
            raise

    async def _safe_health_check(
        self,
        component: str,
        callback: Callable[[], Awaitable[bool]],
    ) -> bool:
        key = f"{component}-health"
        if key in self._timed_out_callbacks:
            self._enforce_timeout_latch()
            return False
        try:
            return await self._run_bounded(key, callback, self._health_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, TimeoutError):
                self._latch_timeout(key)
            self.view.recent_errors.append(
                f"{component}-health: {type(exc).__name__}"
            )
            self.view.safety.degrade(
                RuntimeSafetyState.ENTRY_DISABLED,
                f"{component} health check raised an exception",
            )
            return False

    async def _job_loop(self, job: PeriodicJob) -> None:
        while not self._stop.is_set():
            try:
                await self._run_bounded(f"job:{job.name}", job.callback, job.timeout_seconds)
                await self.metrics.increment(f"job_{job.name}_success_total")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.view.recent_errors.append(f"{job.name}: {type(exc).__name__}")
                self.view.safety.degrade(
                    RuntimeSafetyState.ENTRY_DISABLED, f"job failed: {job.name}"
                )
                await self.metrics.increment(f"job_{job.name}_failure_total")
                if isinstance(exc, TimeoutError):
                    self._latch_timeout(f"job:{job.name}")
                    # The callback could have crossed a durable-write boundary.
                    # Never automatically replay an interrupted dispatch/replay job.
                    return
            await self.clock.sleep(job.interval_seconds)

    async def start(self) -> None:
        if self._started or self._stop.is_set():
            raise RuntimeError("controller instances cannot be started twice")
        self._lock.acquire()
        self._started = True
        try:
            self.view.reconciled = await self.reconcile()
            await self.health_once()
            health_job = PeriodicJob(
                "health", self.config.sentinel.health_seconds, self.health_once,
                timeout_seconds=self._health_timeout_seconds * 3 + 5,
            )
            for job in (health_job, *self._jobs):
                self._tasks.append(
                    asyncio.create_task(self._job_loop(job), name=f"sentinel:{job.name}")
                )
        except BaseException:
            if not any(not task.done() for task in self._active_callbacks.values()):
                self._lock.release()
            else:
                self.view.safety.emergency_stop("startup interrupted with callbacks still running")
            raise

    async def stop(self) -> None:
        self._stop.set()
        self.view.safety.emergency_stop("controller shutdown")
        for task in self._tasks:
            task.cancel()
        active = {task for task in self._active_callbacks.values() if not task.done()}
        for task in active:
            task.cancel()
        pending = active | {task for task in self._tasks if not task.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self._health_timeout_seconds)
        # Include callbacks spawned while the initial cancellation was being
        # delivered; no still-running operation may outlive lock ownership.
        pending |= {task for task in self._active_callbacks.values() if not task.done()}
        for task in self._tasks:
            if task.done() and not task.cancelled():
                task.exception()
        self._tasks = [task for task in self._tasks if not task.done()]
        if pending:
            self.view.safety.emergency_stop(
                "callback cancellation incomplete; instance lock retained until process exit"
            )
            raise RuntimeError(
                "shutdown incomplete: callbacks remain active; instance lock retained"
            )
        self._lock.release()
