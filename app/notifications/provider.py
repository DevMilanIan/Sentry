from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from app.clock.base import Clock
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import TimestampedModel

logger = logging.getLogger(__name__)


class Notification(TimestampedModel):
    notification_id: UUID = Field(default_factory=uuid4)
    environment: ExecutionEnvironment
    demo_backend: DemoBackend | None
    alert_class: str
    severity: int = Field(ge=0, le=5)
    title: str
    message: str
    acknowledged: bool = False

    @model_validator(mode="after")
    def backend_matches_environment(self) -> Notification:
        if self.environment is ExecutionEnvironment.LIVE and self.demo_backend is not None:
            raise ValueError("LIVE notification cannot have a Demo backend")
        if self.environment is ExecutionEnvironment.DEMO and self.demo_backend is None:
            raise ValueError("DEMO notification requires a Demo backend")
        return self


class NotificationSink(Protocol):
    async def __call__(self, notification: Notification) -> object: ...


class LocalNotificationProvider:
    """Local persistence is mandatory; optional remote delivery is best-effort."""

    def __init__(
        self,
        clock: Clock,
        environment: ExecutionEnvironment,
        demo_backend: DemoBackend | None,
        persist: Callable[[str, Notification], Awaitable[UUID]],
        remote_sink: NotificationSink | None = None,
    ) -> None:
        if environment is ExecutionEnvironment.LIVE and demo_backend is not None:
            raise ValueError("LIVE notification provider cannot have a Demo backend")
        if environment is ExecutionEnvironment.DEMO and demo_backend is None:
            raise ValueError("DEMO notification provider requires a Demo backend")
        self._clock = clock
        self._environment = environment
        self._demo_backend = demo_backend
        self._persist = persist
        self._remote_sink = remote_sink

    async def send(self, alert_class: str, severity: int, title: str, message: str) -> Notification:
        label = self._environment.value
        if self._demo_backend:
            label = f"{label}/{self._demo_backend.value}"
        notification = Notification(
            created_at=self._clock.now(),
            environment=self._environment,
            demo_backend=self._demo_backend,
            alert_class=alert_class,
            severity=severity,
            title=f"[{label}] {title}",
            message=message,
        )
        await self._persist("notification_events", notification)
        if self._remote_sink is not None:
            try:
                await self._remote_sink(notification)
            except Exception as exc:
                # Trading correctness never depends on a remote notification service.
                logger.warning("remote notification failed: %s", type(exc).__name__)
        return notification
