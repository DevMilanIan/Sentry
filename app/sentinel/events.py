from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta

from app.clock.base import Clock
from app.domain.models import SentinelEvent


class EventBus:
    """Bounded in-process event bus. Overflow is observable and fails noisy."""

    def __init__(self, maxsize: int = 1_000) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._queue: asyncio.Queue[SentinelEvent] = asyncio.Queue(maxsize=maxsize)
        self.dropped_events = 0

    async def publish(self, event: SentinelEvent) -> None:
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            raise RuntimeError("sentinel event bus is full") from None

    async def receive(self) -> SentinelEvent:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def pending(self) -> int:
        return self._queue.qsize()


class EventDeduplicator:
    def __init__(
        self, clock: Clock, ttl: timedelta = timedelta(hours=24), capacity: int = 20_000
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._clock = clock
        self._ttl = ttl
        self._capacity = capacity
        self._seen: OrderedDict[str, datetime] = OrderedDict()

    def accept(self, key: str) -> bool:
        now = self._clock.now()
        cutoff = now - self._ttl
        for expired_key in tuple(
            item_key for item_key, seen_at in self._seen.items() if seen_at < cutoff
        ):
            self._seen.pop(expired_key)
        if key in self._seen:
            self._seen.move_to_end(key)
            return False
        self._seen[key] = now
        if len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


class FreshnessMonitor:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._observations: dict[str, datetime] = {}

    def observe(self, stream: str, effective_at: datetime) -> None:
        if effective_at.tzinfo is None or effective_at.utcoffset() is None:
            raise ValueError("freshness timestamp must be timezone-aware")
        previous = self._observations.get(stream)
        if previous is not None and effective_at < previous:
            raise ValueError("freshness observation moved backward")
        self._observations[stream] = effective_at

    def age(self, stream: str) -> timedelta | None:
        observed = self._observations.get(stream)
        if observed is None:
            return None
        return self._clock.now() - observed

    def is_fresh(self, stream: str, maximum_age: timedelta) -> bool:
        age = self.age(stream)
        return age is not None and timedelta(0) <= age <= maximum_age
