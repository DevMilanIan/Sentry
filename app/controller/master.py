from __future__ import annotations

import asyncio
import importlib
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

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("periodic job name cannot be empty")
        if self.interval_seconds <= 0:
            raise ValueError("periodic job interval must be positive")


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
    ) -> None:
        self.config = config
        self.view = view
        self.clock = clock
        self.metrics = metrics
        self._repository_health = repository_health
        self._broker_health = broker_health
        self._reconcile = reconcile
        self._execution_health = execution_health
        self._jobs: list[PeriodicJob] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        lock_name = f"{view.binding.environment.value.lower()}.lock"
        self._lock = InstanceLock(config.runtime.instance_lock_dir / lock_name)

    def add_job(self, job: PeriodicJob) -> None:
        if job.name == "health" or any(existing.name == job.name for existing in self._jobs):
            raise ValueError(f"duplicate controller job: {job.name}")
        self._jobs.append(job)

    async def reconcile(self) -> bool:
        self.view.reconciled = False
        try:
            result = await self._reconcile()
        except Exception as exc:
            self.view.recent_errors.append(
                f"reconciliation: {type(exc).__name__}"
            )
            self.view.safety.degrade(
                RuntimeSafetyState.ENTRY_DISABLED,
                "reconciliation raised an exception",
            )
            return False
        self.view.reconciled = result
        if not result:
            self.view.safety.degrade(RuntimeSafetyState.ENTRY_DISABLED, "reconciliation failed")
        return result

    async def health_once(self) -> None:
        kill_switch_clear = (
            not self.config.runtime.environment_execution_disabled
            and not self.config.runtime.disabled_file.exists()
        )
        database_healthy, broker_healthy = await asyncio.gather(
            self._safe_health_check("database", self._repository_health),
            self._safe_health_check("broker", self._broker_health),
        )
        execution_healthy = (
            await self._safe_health_check("execution", self._execution_health)
            if self._execution_health is not None
            else self.view.execution_service_healthy
        )
        self.view.execution_service_healthy = execution_healthy
        evidence = SafetyEvidence(
            database_writable=database_healthy,
            broker_state_known=broker_healthy,
            reconciled=self.view.reconciled,
            market_data_fresh=self.view.market_data_fresh,
            account_data_fresh=broker_healthy,
            execution_service_healthy=execution_healthy,
            kill_switch_clear=kill_switch_clear,
            environment_matches=(
                self.config.execution_environment is self.view.binding.environment
                and self.config.demo_backend is self.view.binding.demo_backend
            ),
            unresolved_submission=self.view.unresolved_submission,
        )
        self.view.database_healthy = database_healthy
        self.view.broker_connected = broker_healthy
        self.view.last_safety_evidence = evidence
        self.view.safety.observe(evidence)
        await self.metrics.gauge(
            "runtime_safety_state", list(RuntimeSafetyState).index(self.view.safety.state)
        )

    async def _safe_health_check(
        self,
        component: str,
        callback: Callable[[], Awaitable[bool]],
    ) -> bool:
        try:
            return await callback()
        except Exception as exc:
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
                await job.callback()
                await self.metrics.increment(f"job_{job.name}_success_total")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.view.recent_errors.append(f"{job.name}: {type(exc).__name__}")
                self.view.safety.degrade(
                    RuntimeSafetyState.ENTRY_DISABLED, f"job failed: {job.name}"
                )
                await self.metrics.increment(f"job_{job.name}_failure_total")
            await self.clock.sleep(job.interval_seconds)

    async def start(self) -> None:
        self._lock.acquire()
        try:
            self.view.reconciled = await self.reconcile()
            await self.health_once()
            health_job = PeriodicJob(
                "health", self.config.sentinel.health_seconds, self.health_once
            )
            for job in (health_job, *self._jobs):
                self._tasks.append(
                    asyncio.create_task(self._job_loop(job), name=f"sentinel:{job.name}")
                )
        except BaseException:
            self._lock.release()
            raise

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self._lock.release()
