from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.clock.base import VirtualClock
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.notifications.provider import LocalNotificationProvider, Notification


@pytest.mark.parametrize(
    ("environment", "demo_backend", "expected_title"),
    [
        (ExecutionEnvironment.LIVE, None, "[LIVE] Feed delayed"),
        (
            ExecutionEnvironment.DEMO,
            DemoBackend.OFFLINE_SIM,
            "[DEMO/OFFLINE_SIM] Feed delayed",
        ),
        (
            ExecutionEnvironment.DEMO,
            DemoBackend.BROKER_SHADOW,
            "[DEMO/BROKER_SHADOW] Feed delayed",
        ),
    ],
)
@pytest.mark.asyncio
async def test_send_persists_the_labeled_notification_before_remote_delivery(
    environment: ExecutionEnvironment,
    demo_backend: DemoBackend | None,
    expected_title: str,
) -> None:
    instant = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    events: list[tuple[str, Notification]] = []

    async def persist(table: str, notification: Notification) -> UUID:
        assert table == "notification_events"
        events.append(("persist", notification))
        return notification.notification_id

    async def remote_sink(notification: Notification) -> None:
        events.append(("remote", notification))

    provider = LocalNotificationProvider(
        clock=VirtualClock(instant),
        environment=environment,
        demo_backend=demo_backend,
        persist=persist,
        remote_sink=remote_sink,
    )

    notification = await provider.send("market_data", 3, "Feed delayed", "quotes are stale")

    assert [event for event, _ in events] == ["persist", "remote"]
    assert events[0][1] is notification
    assert events[1][1] is notification
    assert notification.created_at == instant
    assert notification.environment is environment
    assert notification.demo_backend is demo_backend
    assert notification.alert_class == "market_data"
    assert notification.severity == 3
    assert notification.title == expected_title
    assert notification.message == "quotes are stale"
    assert notification.acknowledged is False


@pytest.mark.asyncio
async def test_remote_sink_failure_does_not_undo_mandatory_local_persistence(
    caplog: pytest.LogCaptureFixture,
) -> None:
    instant = datetime(2026, 9, 3, 14, 30, tzinfo=UTC)
    persisted: list[Notification] = []

    async def persist(_table: str, notification: Notification) -> UUID:
        persisted.append(notification)
        return notification.notification_id

    class FailingRemoteSink:
        async def __call__(self, notification: Notification) -> object:
            del notification
            raise ConnectionError("remote secret must not be logged")

    provider = LocalNotificationProvider(
        clock=VirtualClock(instant),
        environment=ExecutionEnvironment.DEMO,
        demo_backend=DemoBackend.OFFLINE_SIM,
        persist=persist,
        remote_sink=FailingRemoteSink(),
    )

    with caplog.at_level(logging.WARNING, logger="app.notifications.provider"):
        notification = await provider.send("system", 4, "Remote down", "local record required")

    assert persisted == [notification]
    assert "remote notification failed: ConnectionError" in caplog.text
    assert "remote secret must not be logged" not in caplog.text


@pytest.mark.asyncio
async def test_local_persistence_failure_is_fatal_and_skips_remote_delivery() -> None:
    remote_notifications: list[Notification] = []

    async def failing_persist(_table: str, _notification: Notification) -> UUID:
        raise OSError("local store unavailable")

    async def remote_sink(notification: Notification) -> None:
        remote_notifications.append(notification)

    provider = LocalNotificationProvider(
        clock=VirtualClock(datetime(2026, 9, 3, 14, 30, tzinfo=UTC)),
        environment=ExecutionEnvironment.LIVE,
        demo_backend=None,
        persist=failing_persist,
        remote_sink=remote_sink,
    )

    with pytest.raises(OSError, match="local store unavailable"):
        await provider.send("system", 5, "Persistence down", "delivery must stop")

    assert remote_notifications == []


@pytest.mark.parametrize("severity", [-1, 6])
@pytest.mark.asyncio
async def test_invalid_severity_is_rejected_before_any_delivery(severity: int) -> None:
    persisted: list[Notification] = []
    remotely_delivered: list[Notification] = []

    async def persist(_table: str, notification: Notification) -> UUID:
        persisted.append(notification)
        return notification.notification_id

    async def remote_sink(notification: Notification) -> None:
        remotely_delivered.append(notification)

    provider = LocalNotificationProvider(
        clock=VirtualClock(datetime(2026, 9, 3, 14, 30, tzinfo=UTC)),
        environment=ExecutionEnvironment.DEMO,
        demo_backend=DemoBackend.OFFLINE_SIM,
        persist=persist,
        remote_sink=remote_sink,
    )

    with pytest.raises(ValidationError):
        await provider.send("system", severity, "Invalid", "must not be delivered")

    assert persisted == []
    assert remotely_delivered == []


@pytest.mark.parametrize("severity", [0, 5])
@pytest.mark.asyncio
async def test_severity_validation_accepts_both_inclusive_boundaries(severity: int) -> None:
    async def persist(_table: str, notification: Notification) -> UUID:
        return notification.notification_id

    provider = LocalNotificationProvider(
        clock=VirtualClock(datetime(2026, 9, 3, 14, 30, tzinfo=UTC)),
        environment=ExecutionEnvironment.LIVE,
        demo_backend=None,
        persist=persist,
    )

    notification = await provider.send("system", severity, "Boundary", "valid severity")

    assert notification.severity == severity


@pytest.mark.parametrize(
    "demo_backend",
    [DemoBackend.OFFLINE_SIM, DemoBackend.BROKER_SHADOW],
)
def test_notification_rejects_live_environment_with_demo_backend(
    demo_backend: DemoBackend,
) -> None:
    with pytest.raises(ValidationError, match="LIVE notification cannot have a Demo backend"):
        Notification(
            created_at=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
            environment=ExecutionEnvironment.LIVE,
            demo_backend=demo_backend,
            alert_class="system",
            severity=3,
            title="[LIVE] Invalid binding",
            message="must not be constructible",
        )


@pytest.mark.parametrize(
    "demo_backend",
    [DemoBackend.OFFLINE_SIM, DemoBackend.BROKER_SHADOW],
)
def test_provider_rejects_live_demo_binding_before_persistence(
    demo_backend: DemoBackend,
) -> None:
    persisted: list[Notification] = []

    async def persist(_table: str, notification: Notification) -> UUID:
        persisted.append(notification)
        return notification.notification_id

    with pytest.raises(ValueError, match="LIVE notification provider cannot have a Demo backend"):
        LocalNotificationProvider(
            clock=VirtualClock(datetime(2026, 9, 3, 14, 30, tzinfo=UTC)),
            environment=ExecutionEnvironment.LIVE,
            demo_backend=demo_backend,
            persist=persist,
        )

    assert persisted == []


def test_notification_rejects_demo_environment_without_backend() -> None:
    with pytest.raises(ValidationError, match="DEMO notification requires a Demo backend"):
        Notification(
            created_at=datetime(2026, 9, 3, 14, 30, tzinfo=UTC),
            environment=ExecutionEnvironment.DEMO,
            demo_backend=None,
            alert_class="system",
            severity=3,
            title="[DEMO] Invalid binding",
            message="must not be constructible",
        )


def test_provider_rejects_demo_environment_without_backend() -> None:
    async def persist(_table: str, notification: Notification) -> UUID:
        return notification.notification_id

    with pytest.raises(ValueError, match="DEMO notification provider requires a Demo backend"):
        LocalNotificationProvider(
            clock=VirtualClock(datetime(2026, 9, 3, 14, 30, tzinfo=UTC)),
            environment=ExecutionEnvironment.DEMO,
            demo_backend=None,
            persist=persist,
        )
