from __future__ import annotations

import inspect
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.clock.base import VirtualClock
from app.domain.models import canonical_json, sha256_json


class ReplayMode(StrEnum):
    DETERMINISTIC_REGRESSION = "deterministic_regression"
    REASONING = "reasoning"


class OutcomeHorizon(StrEnum):
    INTRADAY = "intraday"
    END_OF_DAY = "end_of_day"
    NEXT_SESSION = "next_session"
    MULTI_DAY = "multi_day"


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """An observation becomes visible at ``available_at``.

    ``effective_at`` records when the underlying fact/market state applied;
    delayed data therefore has an earlier effective time and a later available
    time. Replay ordering and visibility always use availability first.
    """

    event_id: str
    kind: str
    effective_at: datetime
    available_at: datetime
    payload: Any
    sequence: int = 0

    def __post_init__(self) -> None:
        if not self.event_id or not self.kind:
            raise ValueError("replay events require an id and kind")
        if self.effective_at.tzinfo is None or self.effective_at.utcoffset() is None:
            raise ValueError("effective_at must be timezone-aware")
        if self.available_at.tzinfo is None or self.available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if self.available_at.astimezone(UTC) < self.effective_at.astimezone(UTC):
            raise ValueError("an observation cannot be available before it is effective")
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")

    @property
    def causal_key(self) -> tuple[datetime, int, datetime, str]:
        return (
            self.available_at.astimezone(UTC),
            self.sequence,
            self.effective_at.astimezone(UTC),
            self.event_id,
        )

    @property
    def content_hash(self) -> str:
        return sha256_json(
            {
                "event_id": self.event_id,
                "kind": self.kind,
                "effective_at": self.effective_at.astimezone(UTC),
                "available_at": self.available_at.astimezone(UTC),
                "payload": self.payload,
                "sequence": self.sequence,
            }
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    mode: ReplayMode
    strategy_version: str
    config_version: str
    event_ids: tuple[str, ...]
    output_hashes: tuple[str, ...]
    replay_hash: str
    started_at: datetime
    ended_at: datetime


class CausalReplayContext:
    """Read view that contains only events delivered by the virtual clock."""

    def __init__(self, clock: VirtualClock) -> None:
        self._clock = clock
        self._events: list[ReplayEvent] = []
        self._by_kind: dict[str, list[ReplayEvent]] = defaultdict(list)

    @property
    def now(self) -> datetime:
        return self._clock.now()

    def observe(self, event: ReplayEvent) -> None:
        if event.available_at.astimezone(UTC) > self.now:
            raise ValueError("future event cannot enter the causal replay view")
        if any(existing.event_id == event.event_id for existing in self._events):
            raise ValueError(f"duplicate replay event id: {event.event_id}")
        self._events.append(event)
        self._by_kind[event.kind].append(event)

    def events(self, kind: str | None = None) -> tuple[ReplayEvent, ...]:
        values = self._events if kind is None else self._by_kind.get(kind, [])
        # Defense in depth: even if context construction changes later, never
        # disclose an observation that was not available at this instant.
        return tuple(event for event in values if event.available_at.astimezone(UTC) <= self.now)

    def latest(self, kind: str) -> ReplayEvent | None:
        events = self.events(kind)
        return events[-1] if events else None

    def require_visible(self, event_id: str) -> ReplayEvent:
        for event in self.events():
            if event.event_id == event_id:
                return event
        raise LookupError(f"event is not causally visible: {event_id}")


ReplayHandler = Callable[[ReplayEvent, CausalReplayContext], Any | Awaitable[Any]]


class ReplayEngine:
    """Strict event-time runner with deterministic ordering and result hashes."""

    _horizon_delays: Mapping[OutcomeHorizon, timedelta] = {
        OutcomeHorizon.INTRADAY: timedelta(hours=2),
        OutcomeHorizon.END_OF_DAY: timedelta(hours=8),
        OutcomeHorizon.NEXT_SESSION: timedelta(days=1),
        OutcomeHorizon.MULTI_DAY: timedelta(days=5),
    }

    def __init__(
        self,
        clock: VirtualClock,
        *,
        mode: ReplayMode = ReplayMode.DETERMINISTIC_REGRESSION,
        strategy_version: str,
        config_version: str,
        handlers: Mapping[str, ReplayHandler] | None = None,
    ) -> None:
        if not strategy_version or not config_version:
            raise ValueError("replay versions are required")
        self._clock = clock
        self._mode = mode
        self._strategy_version = strategy_version
        self._config_version = config_version
        self._handlers = dict(handlers or {})
        self._context = CausalReplayContext(clock)

    @property
    def context(self) -> CausalReplayContext:
        return self._context

    async def run(self, events: Iterable[ReplayEvent]) -> ReplayResult:
        ordered = sorted(events, key=lambda item: item.causal_key)
        ids = [event.event_id for event in ordered]
        if len(ids) != len(set(ids)):
            raise ValueError("replay event ids must be unique")
        if ordered and ordered[0].available_at.astimezone(UTC) < self._clock.now():
            raise ValueError("replay cannot move the virtual clock backward")

        started = self._clock.now()
        outputs: list[str] = []
        for event in ordered:
            await self._clock.advance_to(event.available_at)
            self._context.observe(event)
            handler = self._handlers.get(event.kind)
            if handler is None:
                continue
            value = handler(event, self._context)
            if inspect.isawaitable(value):
                value = await value
            outputs.append(sha256_json(value))

        ended = self._clock.now()
        replay_hash = sha256_json(
            {
                "mode": self._mode.value,
                "strategy_version": self._strategy_version,
                "config_version": self._config_version,
                "events": [event.content_hash for event in ordered],
                "outputs": outputs,
            }
        )
        return ReplayResult(
            mode=self._mode,
            strategy_version=self._strategy_version,
            config_version=self._config_version,
            event_ids=tuple(ids),
            output_hashes=tuple(outputs),
            replay_hash=replay_hash,
            started_at=started,
            ended_at=ended,
        )

    @classmethod
    def outcome_due_at(cls, decision_time: datetime, horizon: OutcomeHorizon) -> datetime:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision time must be timezone-aware")
        return decision_time.astimezone(UTC) + cls._horizon_delays[horizon]

    @staticmethod
    def canonical_output(value: Any) -> str:
        """Expose canonical encoding for exact regression fixture assertions."""

        return canonical_json(value)
