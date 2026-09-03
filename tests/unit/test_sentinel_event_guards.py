from __future__ import annotations

from datetime import timedelta

import pytest

from app.clock.base import VirtualClock
from app.domain.models import SentinelEvent
from app.sentinel.events import EventBus, EventDeduplicator, FreshnessMonitor


def _event(clock: VirtualClock, key: str) -> SentinelEvent:
    return SentinelEvent(
        created_at=clock.now(),
        event_type="test.event",
        source="unit-test",
        effective_at=clock.now(),
        severity=1,
        deduplication_key=key,
    )


@pytest.mark.parametrize("maxsize", [0, -1])
def test_event_bus_rejects_unbounded_queue_sizes(maxsize: int) -> None:
    with pytest.raises(ValueError, match="maxsize must be positive"):
        EventBus(maxsize=maxsize)


@pytest.mark.asyncio
async def test_event_bus_overflow_is_noisy_bounded_and_fifo(clock: VirtualClock) -> None:
    bus = EventBus(maxsize=2)
    first = _event(clock, "first")
    second = _event(clock, "second")

    await bus.publish(first)
    await bus.publish(second)
    with pytest.raises(RuntimeError, match="event bus is full"):
        await bus.publish(_event(clock, "overflow"))

    assert bus.pending == 2
    assert bus.dropped_events == 1
    assert await bus.receive() == first
    bus.task_done()
    assert await bus.receive() == second
    bus.task_done()
    assert bus.pending == 0


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(microseconds=-1)])
def test_deduplicator_requires_positive_ttl(
    clock: VirtualClock, ttl: timedelta
) -> None:
    with pytest.raises(ValueError, match="ttl must be positive"):
        EventDeduplicator(clock, ttl=ttl)


@pytest.mark.parametrize("capacity", [0, -1])
def test_deduplicator_requires_positive_capacity(
    clock: VirtualClock, capacity: int
) -> None:
    with pytest.raises(ValueError, match="capacity must be positive"):
        EventDeduplicator(clock, capacity=capacity)


@pytest.mark.asyncio
async def test_deduplicator_honors_ttl_boundary(clock: VirtualClock) -> None:
    deduplicator = EventDeduplicator(clock, ttl=timedelta(seconds=10), capacity=10)

    assert deduplicator.accept("event")
    assert not deduplicator.accept("event")
    await clock.advance(timedelta(seconds=10))
    assert not deduplicator.accept("event")
    await clock.advance(timedelta(microseconds=1))
    assert deduplicator.accept("event")


def test_deduplicator_evicts_least_recently_seen_at_capacity(
    clock: VirtualClock,
) -> None:
    deduplicator = EventDeduplicator(clock, capacity=2)

    assert deduplicator.accept("a")
    assert deduplicator.accept("b")
    assert not deduplicator.accept("a")
    assert deduplicator.accept("c")

    assert deduplicator.accept("b")
    assert not deduplicator.accept("c")


@pytest.mark.asyncio
async def test_deduplicator_expires_key_even_after_recent_key_was_reordered(
    clock: VirtualClock,
) -> None:
    deduplicator = EventDeduplicator(clock, ttl=timedelta(seconds=10), capacity=10)

    assert deduplicator.accept("older")
    await clock.advance(timedelta(seconds=5))
    assert deduplicator.accept("newer")
    assert not deduplicator.accept("older")
    await clock.advance(timedelta(seconds=6))

    assert deduplicator.accept("older")


def test_freshness_unknown_stream_fails_closed(clock: VirtualClock) -> None:
    monitor = FreshnessMonitor(clock)

    assert monitor.age("quotes") is None
    assert not monitor.is_fresh("quotes", timedelta(seconds=30))


@pytest.mark.asyncio
async def test_freshness_boundary_then_staleness(clock: VirtualClock) -> None:
    monitor = FreshnessMonitor(clock)
    monitor.observe("quotes", clock.now())

    await clock.advance(timedelta(seconds=30))
    assert monitor.age("quotes") == timedelta(seconds=30)
    assert monitor.is_fresh("quotes", timedelta(seconds=30))

    await clock.advance(timedelta(microseconds=1))
    assert not monitor.is_fresh("quotes", timedelta(seconds=30))


def test_future_freshness_observation_never_counts_as_fresh(clock: VirtualClock) -> None:
    monitor = FreshnessMonitor(clock)
    monitor.observe("quotes", clock.now() + timedelta(seconds=1))

    assert monitor.age("quotes") == timedelta(seconds=-1)
    assert not monitor.is_fresh("quotes", timedelta(seconds=30))


@pytest.mark.asyncio
async def test_backward_observation_is_rejected_without_replacing_latest(
    clock: VirtualClock,
) -> None:
    monitor = FreshnessMonitor(clock)
    initial = clock.now()
    monitor.observe("quotes", initial)
    await clock.advance(timedelta(seconds=5))
    monitor.observe("quotes", clock.now())

    with pytest.raises(ValueError, match="moved backward"):
        monitor.observe("quotes", initial + timedelta(seconds=4))

    assert monitor.age("quotes") == timedelta(0)
    assert monitor.is_fresh("quotes", timedelta(0))


def test_naive_freshness_observation_is_rejected(clock: VirtualClock) -> None:
    monitor = FreshnessMonitor(clock)

    with pytest.raises(ValueError, match="timezone-aware"):
        monitor.observe("quotes", clock.now().replace(tzinfo=None))
