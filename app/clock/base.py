from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime:
        """Return an aware UTC timestamp."""

    @abstractmethod
    async def sleep(self, seconds: float) -> None:
        """Wait or advance by the requested duration."""


class RealClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class VirtualClock(Clock):
    """Controllable clock for causally correct replay and deterministic tests."""

    def __init__(self, initial: datetime) -> None:
        if initial.tzinfo is None or initial.utcoffset() is None:
            raise ValueError("VirtualClock initial time must be timezone-aware")
        self._current = initial.astimezone(UTC)
        self._condition = asyncio.Condition()

    def now(self) -> datetime:
        return self._current

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("clock cannot move backward")
        await self.advance(timedelta(seconds=seconds))

    async def advance(self, delta: timedelta) -> None:
        if delta.total_seconds() < 0:
            raise ValueError("clock cannot move backward")
        async with self._condition:
            self._current += delta
            self._condition.notify_all()

    async def advance_to(self, target: datetime) -> None:
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("target must be timezone-aware")
        target_utc = target.astimezone(UTC)
        if target_utc < self._current:
            raise ValueError("clock cannot move backward")
        await self.advance(target_utc - self._current)
