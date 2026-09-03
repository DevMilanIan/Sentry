from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import groupby
from typing import Any, Literal, Protocol
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, Field, field_validator

from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import DomainModel, EquityQuote, OptionQuote, SentinelEvent, sha256_json
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.market.models import PriceBar, ReplayFixture, ReplayRecord
from app.market.replay import OfflineReplayMarketDataProvider
from app.sentinel.events import EventBus, FreshnessMonitor


class ReplayAuditRepository(Protocol):
    binding: RuntimeBinding

    async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID: ...


class OfflineReplayCheckpoint(DomainModel):
    version: Literal["offline-replay-checkpoint-v1"] = "offline-replay-checkpoint-v1"
    data_mode: Literal["REPLAY"] = "REPLAY"
    namespace: str = Field(min_length=1)
    fixture_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fixture_version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    capability_version: str = Field(min_length=1)
    last_sequence: int | None = Field(default=None, ge=0)
    replay_time: datetime
    complete: bool = False

    @field_validator("replay_time")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("replay_time must be timezone-aware")
        return value.astimezone(UTC)


class OfflineReplayStep(DomainModel):
    processed_sequences: tuple[int, ...]
    events: tuple[SentinelEvent, ...]
    option_quotes: tuple[OptionQuote, ...]
    checkpoint: OfflineReplayCheckpoint


type CheckpointSink = Callable[[OfflineReplayCheckpoint], Awaitable[None]]
type EventHandler = Callable[[SentinelEvent], Awaitable[None]]
type QuoteConsumer = Callable[[OptionQuote], Awaitable[object]]
type MarketObservation = EquityQuote | OptionQuote | PriceBar


@dataclass
class _PendingRecord:
    record: ReplayRecord
    value: MarketObservation
    event: SentinelEvent
    snapshot_persisted: bool = False
    event_persisted: bool = False
    event_enqueued: bool = False
    event_received: bool = False
    event_dispatched: bool = False


class OfflineReplaySession:
    """A caller-paced, credential-free replay worker with a durable group cursor.

    Schedule ``step()`` with a wall clock; each call advances the injected trading
    clock through only one timestamp group. There is no automatic fixture loop.
    ``market`` exposes only observations available at that virtual time.

    Without an event handler, callers must drain ``event_bus``. With a handler,
    this worker owns bus consumption and dispatches synchronously. Queue overflow
    and callback/persistence failures propagate; no failed group is checkpointed.
    Successful side effects are remembered during in-process retries. Across a
    crash, uncheckpointed effects may replay with the same stable identities.
    Therefore callbacks must be idempotent, and checkpoint_sink should atomically
    persist the supplied cursor together with any broker state they changed.

    Restore using the same fixture and namespace. The virtual clock may be at the
    checkpoint time or up to the next group time, allowing a newer durable broker
    snapshot from an interrupted group. Freshness streams use ``market_data``,
    ``equity_quote:SYMBOL``,
    ``option_quote:INSTRUMENT_ID`` and ``bar:SYMBOL:TIMEFRAME``. Freshness is always
    measured in replay time, not represented as live-market freshness.
    """

    def __init__(
        self,
        fixture: ReplayFixture,
        clock: VirtualClock,
        repository: ReplayAuditRepository,
        checkpoint_sink: CheckpointSink,
        *,
        event_handler: EventHandler | None = None,
        quote_consumer: QuoteConsumer | None = None,
        checkpoint: OfflineReplayCheckpoint | None = None,
        namespace: str | None = None,
        run_id: UUID | None = None,
        max_records_per_step: int = 1_000,
        event_bus_maxsize: int = 1_000,
    ) -> None:
        if not isinstance(clock, VirtualClock):
            raise TypeError("offline replay requires an injected VirtualClock")
        if max_records_per_step <= 0:
            raise ValueError("max_records_per_step must be positive")
        binding = repository.binding
        if (
            binding.environment is not ExecutionEnvironment.DEMO
            or binding.demo_backend is not DemoBackend.OFFLINE_SIM
            or binding.external_write_authority
        ):
            raise SafetyCriticalError("replay requires a no-write DEMO/OFFLINE_SIM repository")
        namespace = binding.idempotency_namespace if namespace is None else namespace
        if not namespace.strip():
            raise ValueError("namespace cannot be empty")
        if namespace != binding.idempotency_namespace:
            raise SafetyCriticalError("replay namespace does not match repository binding")
        ordered = tuple(sorted(fixture.records, key=lambda item: item.sequence))
        source = fixture.model_copy(update={"records": ordered})
        self.fixture_hash = sha256_json(source.model_dump(mode="json"))
        self._fixture, self._values = self._normalize(source)
        self._records = self._fixture.records
        if any(
            sum(1 for _ in group) > max_records_per_step
            for _, group in groupby(self._records, key=lambda item: item.available_at)
        ):
            raise DataInvalidError("replay timestamp group exceeds max_records_per_step")
        self.clock = clock
        self.market = OfflineReplayMarketDataProvider(self._fixture, clock)
        self.event_bus = EventBus(maxsize=event_bus_maxsize)
        self.freshness = FreshnessMonitor(clock)
        self._freshness_times: dict[str, datetime] = {}
        self._repository = repository
        self._checkpoint_sink = checkpoint_sink
        self._event_handler = event_handler
        self._quote_consumer = quote_consumer
        self._namespace = namespace
        self._run_id = run_id
        self._lock = asyncio.Lock()
        self._pending: list[_PendingRecord] = []
        self._consumed_quotes: set[UUID] = set()
        self._next_index = 0
        initial = OfflineReplayCheckpoint(
            namespace=namespace,
            fixture_hash=self.fixture_hash,
            fixture_version=fixture.version,
            provider=fixture.provider,
            capability_version=fixture.capability_version,
            replay_time=clock.now(),
        )
        self._checkpoint = initial if checkpoint is None else checkpoint
        self._restore(initial)

    @property
    def provider(self) -> OfflineReplayMarketDataProvider:
        return self.market

    @property
    def events(self) -> EventBus:
        return self.event_bus

    @property
    def checkpoint(self) -> OfflineReplayCheckpoint:
        return self._checkpoint

    @property
    def complete(self) -> bool:
        return self._checkpoint.complete

    @property
    def last_sequence(self) -> int | None:
        return self._checkpoint.last_sequence

    async def step(self) -> OfflineReplayStep:
        async with self._lock:
            if self.complete:
                return self._result(())
            if not self._pending and self._next_index < len(self._records):
                available_at = self._records[self._next_index].available_at
                if self.clock.now() > available_at:
                    raise DataInvalidError("replay clock passed the next unprocessed observation")
                await self.clock.advance_to(available_at)
                for record in self._records[self._next_index :]:
                    if record.available_at != available_at:
                        break
                    value = self._values[record.sequence]
                    self._pending.append(_PendingRecord(record, value, self._event(record, value)))
            if self._pending and self.clock.now() != self._pending[0].record.available_at:
                raise DataInvalidError("replay clock changed during an uncheckpointed group")
            for pending in self._pending:
                await self._process(pending)
                if self.clock.now() != pending.record.available_at:
                    raise DataInvalidError("replay callback changed the trading clock")
            count = len(self._pending)
            candidate = self._checkpoint.model_copy(
                update={
                    "last_sequence": self._pending[-1].record.sequence
                    if self._pending
                    else self._checkpoint.last_sequence,
                    "replay_time": self.clock.now(),
                    "complete": self._next_index + count == len(self._records),
                }
            )
            await self._checkpoint_sink(candidate)
            self._checkpoint = candidate
            completed = tuple(self._pending)
            self._next_index += count
            self._pending.clear()
            return self._result(completed)

    def _normalize(
        self, fixture: ReplayFixture
    ) -> tuple[ReplayFixture, dict[int, MarketObservation]]:
        values: dict[int, MarketObservation] = {}
        records: list[ReplayRecord] = []
        quote_hashes: dict[UUID, str] = {}
        for record in fixture.records:
            payload = dict(record.payload)
            if record.kind != "bar":
                payload.setdefault(
                    "snapshot_id",
                    str(uuid5(NAMESPACE_URL, f"{self.fixture_hash}:{record.sequence}")),
                )
            value: MarketObservation
            if record.kind == "equity_quote":
                value = EquityQuote.model_validate(payload)
            elif record.kind == "option_quote":
                value = OptionQuote.model_validate(payload)
            else:
                value = PriceBar.model_validate(payload)
            normalized = value.model_dump(mode="json")
            if isinstance(value, EquityQuote | OptionQuote):
                fingerprint = sha256_json(normalized)
                if (
                    value.snapshot_id in quote_hashes
                    and quote_hashes[value.snapshot_id] != fingerprint
                ):
                    raise DataInvalidError("replay snapshot identity has conflicting payloads")
                quote_hashes[value.snapshot_id] = fingerprint
            records.append(record.model_copy(update={"payload": normalized}))
            values[record.sequence] = value
        return fixture.model_copy(update={"records": tuple(records)}), values

    def _restore(self, initial: OfflineReplayCheckpoint) -> None:
        saved = self._checkpoint
        for field in (
            "namespace",
            "fixture_hash",
            "fixture_version",
            "provider",
            "capability_version",
        ):
            if getattr(saved, field) != getattr(initial, field):
                raise DataInvalidError(f"replay checkpoint {field} does not match")
        if self.clock.now() < saved.replay_time:
            raise DataInvalidError("restore clock precedes checkpoint replay_time")
        if saved.complete and self.clock.now() != saved.replay_time:
            raise DataInvalidError("completed replay clock must equal checkpoint replay_time")
        if saved.last_sequence is not None:
            index = next(
                (i for i, item in enumerate(self._records) if item.sequence == saved.last_sequence),
                None,
            )
            if index is None:
                raise DataInvalidError("replay checkpoint sequence is not in fixture")
            self._next_index = index + 1
            if saved.replay_time != self._records[index].available_at:
                raise DataInvalidError("replay checkpoint time does not match sequence")
            if (
                self._next_index < len(self._records)
                and self._records[self._next_index].available_at == saved.replay_time
            ):
                raise DataInvalidError("replay checkpoint splits a timestamp group")
            if saved.complete != (self._next_index == len(self._records)):
                raise DataInvalidError("replay checkpoint completion does not match sequence")
        elif saved.complete and self._records:
            raise DataInvalidError("nonempty replay cannot be complete without a sequence")
        if (
            saved.last_sequence is None
            and self._records
            and saved.replay_time > self._records[0].available_at
        ):
            raise DataInvalidError("initial checkpoint time exceeds first observation")
        if (
            self._next_index < len(self._records)
            and self.clock.now() > self._records[self._next_index].available_at
        ):
            raise DataInvalidError("replay clock passed the next unprocessed observation")
        for record in self._records[: self._next_index]:
            value = self._values[record.sequence]
            self._observe(value)
            if isinstance(value, OptionQuote):
                self._consumed_quotes.add(value.snapshot_id)

    def _context(self, record: ReplayRecord) -> dict[str, Any]:
        return {
            "data_mode": "REPLAY",
            "environment": "DEMO",
            "demo_backend": "OFFLINE_SIM",
            "namespace": self._namespace,
            "run_id": str(self._run_id) if self._run_id is not None else None,
            "fixture_hash": self.fixture_hash,
            "fixture_version": self._fixture.version,
            "replay_sequence": record.sequence,
            "replay_kind": record.kind,
            "deduplication_key": (
                f"offline-replay:{self._namespace}:{self.fixture_hash}:{record.sequence}:{record.kind}"
            ),
        }

    def _event(self, record: ReplayRecord, value: MarketObservation) -> SentinelEvent:
        context = self._context(record)
        symbol = value.contract.symbol if isinstance(value, OptionQuote) else value.symbol
        reference = (
            str(value.snapshot_id)
            if isinstance(value, EquityQuote | OptionQuote)
            else str(uuid5(NAMESPACE_URL, context["deduplication_key"]))
        )
        return SentinelEvent(
            event_id=uuid5(NAMESPACE_URL, context["deduplication_key"]),
            created_at=record.observed_at,
            event_type="REPLAY_MARKET_OBSERVATION",
            source=self._fixture.provider,
            effective_at=record.effective_at,
            tickers=(symbol,),
            severity=0,
            deduplication_key=context["deduplication_key"],
            raw_reference_ids=(reference,),
            payload=context,
        )

    async def _process(self, pending: _PendingRecord) -> None:
        record, value, event = pending.record, pending.value, pending.event
        if not pending.snapshot_persisted:
            table = "option_snapshots" if isinstance(value, OptionQuote) else "market_snapshots"
            await self._repository.append(
                table,
                {
                    **value.model_dump(mode="json"),
                    **self._context(record),
                    "created_at": record.observed_at.isoformat(),
                },
            )
            pending.snapshot_persisted = True
        if not pending.event_persisted:
            await self._repository.append(
                "sentinel_events",
                {
                    **event.model_dump(mode="json"),
                    "environment": "DEMO",
                    "namespace": self._namespace,
                    "run_id": str(self._run_id) if self._run_id is not None else None,
                },
            )
            pending.event_persisted = True
        if isinstance(value, OptionQuote) and value.snapshot_id not in self._consumed_quotes:
            if self._quote_consumer is not None:
                await self._quote_consumer(value)
            self._consumed_quotes.add(value.snapshot_id)
        self._observe(value)
        if not pending.event_dispatched:
            if not pending.event_enqueued:
                await self.event_bus.publish(event)
                pending.event_enqueued = True
            if self._event_handler is not None:
                if not pending.event_received:
                    received = await self.event_bus.receive()
                    if received.event_id != event.event_id:
                        self.event_bus.task_done()
                        raise RuntimeError("replay worker requires exclusive bus consumption")
                    pending.event_received = True
                await self._event_handler(event)
                self.event_bus.task_done()
            pending.event_dispatched = True

    def _observe(self, value: MarketObservation) -> None:
        if isinstance(value, OptionQuote):
            stream = f"option_quote:{value.contract.instrument_id}"
        elif isinstance(value, EquityQuote):
            stream = f"equity_quote:{value.symbol}"
        else:
            stream = f"bar:{value.symbol}:{value.timeframe}"
        for name in ("market_data", stream):
            effective_at = value.metadata.effective_at
            previous = self._freshness_times.get(name)
            if previous is None or effective_at > previous:
                self.freshness.observe(name, effective_at)
                self._freshness_times[name] = effective_at

    def _result(self, records: tuple[_PendingRecord, ...]) -> OfflineReplayStep:
        return OfflineReplayStep(
            processed_sequences=tuple(item.record.sequence for item in records),
            events=tuple(item.event for item in records),
            option_quotes=tuple(
                item.value for item in records if isinstance(item.value, OptionQuote)
            ),
            checkpoint=self._checkpoint,
        )
