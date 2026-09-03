from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel

from app.broker.simulated import SimulatedBroker
from app.clock.base import VirtualClock
from app.config import RuntimeBinding
from app.db.repository import InMemoryAuditRepository
from app.domain.enums import DemoBackend, ExecutionEnvironment
from app.domain.models import OptionQuote, SentinelEvent
from app.exceptions import DataInvalidError, SafetyCriticalError
from app.market.fixtures import bundled_fixture_path
from app.market.models import ReplayFixture
from app.market.replay import LookaheadViolationError, MarketDataNotFoundError
from app.sentinel.offline import OfflineReplayCheckpoint, OfflineReplaySession


@pytest.fixture
def fixture() -> ReplayFixture:
    return ReplayFixture.model_validate_json(
        bundled_fixture_path("offline_e2e_session.json").read_text(encoding="utf-8")
    )


class Recorder:
    def __init__(self) -> None:
        self.checkpoints: list[OfflineReplayCheckpoint] = []
        self.events: list[SentinelEvent] = []
        self.quotes: list[OptionQuote] = []

    async def checkpoint(self, value: OfflineReplayCheckpoint) -> None:
        self.checkpoints.append(value)

    async def event(self, value: SentinelEvent) -> None:
        self.events.append(value)

    async def quote(self, value: OptionQuote) -> None:
        self.quotes.append(value)


def make_session(
    fixture: ReplayFixture,
    binding: RuntimeBinding,
    recorder: Recorder,
) -> OfflineReplaySession:
    return OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at - timedelta(seconds=1)),
        InMemoryAuditRepository(binding),
        recorder.checkpoint,
        event_handler=recorder.event,
        quote_consumer=recorder.quote,
    )


async def test_one_causal_timestamp_group_per_step_and_no_automatic_loop(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    assert session.market.capabilities.replay
    assert session.provider is session.market
    assert session.events is session.event_bus
    with pytest.raises(MarketDataNotFoundError):
        await session.market.get_equity_quote("ACME")
    first = await session.step()
    assert first.processed_sequences == (0,)
    assert session.clock.now() == fixture.records[0].available_at
    assert await session.market.get_option_chain("ACME") == ()
    with pytest.raises(LookaheadViolationError):
        await session.market.get_equity_quote("ACME", as_of=fixture.records[-1].available_at)
    second = await session.step()
    assert second.processed_sequences == (1, 2)
    assert len(second.option_quotes) == 2
    assert session.checkpoint.last_sequence == 2
    assert session.freshness.is_fresh("market_data", timedelta(seconds=0))
    assert session.event_bus.pending == 0
    for expected in (3, 4, 5):
        assert (await session.step()).processed_sequences == (expected,)
    assert session.complete
    assert len(recorder.events) == 6
    assert len(recorder.quotes) == 5
    assert len(recorder.checkpoints) == 5
    final_time = session.clock.now()
    assert (await session.step()).processed_sequences == ()
    assert session.clock.now() == final_time
    assert len(recorder.checkpoints) == 5


async def test_persistence_is_json_normalized_and_explicitly_replay_labeled(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    repository = InMemoryAuditRepository(demo_binding)
    recorder = Recorder()
    run_id = uuid4()
    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        repository,
        recorder.checkpoint,
        event_handler=recorder.event,
        run_id=run_id,
    )
    await session.step()
    await session.step()
    snapshots = await repository.list("market_snapshots")
    options = await repository.list("option_snapshots")
    events = await repository.list("sentinel_events")
    assert len(snapshots) == 1 and len(options) == 2 and len(events) == 3
    for row in (*snapshots, *options):
        payload = row["payload"]
        assert payload["data_mode"] == "REPLAY"
        assert payload["environment"] == "DEMO"
        assert payload["demo_backend"] == "OFFLINE_SIM"
        assert payload["fixture_hash"] == session.fixture_hash
        assert payload["run_id"] == str(run_id)
        assert isinstance(payload["bid"], str)
        assert isinstance(payload["snapshot_id"], str)
        json.dumps(payload)
    for row in events:
        payload = row["payload"]
        assert row["run_id"] == run_id
        assert payload["environment"] == "DEMO"
        assert payload["event_type"] == "REPLAY_MARKET_OBSERVATION"
        assert payload["severity"] == 0
        assert payload["payload"]["data_mode"] == "REPLAY"
        assert payload["deduplication_key"] == payload["payload"]["deduplication_key"]
        assert payload["raw_reference_ids"]


async def test_restore_continues_cursor_and_rebuilds_freshness_without_redelivery(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    await session.step()
    await session.step()
    saved = OfflineReplayCheckpoint.model_validate_json(session.checkpoint.model_dump_json())
    restored = OfflineReplaySession(
        fixture,
        VirtualClock(saved.replay_time),
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
        checkpoint=saved,
        quote_consumer=recorder.quote,
        event_handler=recorder.event,
    )
    assert restored.freshness.is_fresh("market_data", timedelta(0))
    assert restored.freshness.age("equity_quote:ACME") == timedelta(seconds=1)
    assert (await restored.step()).processed_sequences == (3,)
    assert len(recorder.quotes) == 3
    assert len(recorder.events) == 4


async def test_broker_snapshot_ahead_of_checkpoint_recovers_interrupted_group(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    clock = VirtualClock(fixture.records[0].available_at)
    broker = SimulatedBroker(clock=clock, namespace="replay-crash")
    quote_calls = 0

    async def consume_then_interrupt(quote: OptionQuote) -> object:
        nonlocal quote_calls
        quote_calls += 1
        if quote_calls == 2:
            raise RuntimeError("process interrupted after first option quote")
        return await broker.consume_quote(quote)

    session = OfflineReplaySession(
        fixture,
        clock,
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
        quote_consumer=consume_then_interrupt,
        event_handler=recorder.event,
    )
    await session.step()
    committed = session.checkpoint
    with pytest.raises(RuntimeError, match="interrupted"):
        await session.step()
    ledger_state = broker.export_state()
    assert ledger_state.recorded_at > committed.replay_time
    assert session.checkpoint == committed
    recovered_clock = VirtualClock(ledger_state.recorded_at)
    recovered_broker = SimulatedBroker(
        clock=recovered_clock, namespace="replay-crash", initial_state=ledger_state
    )
    recovered = OfflineReplaySession(
        fixture,
        recovered_clock,
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
        checkpoint=committed,
        quote_consumer=recovered_broker.consume_quote,
        event_handler=recorder.event,
    )
    result = await recovered.step()
    assert result.processed_sequences == (1, 2)
    assert recovered_clock.now() == ledger_state.recorded_at
    assert len(recovered_broker.export_state().quotes) == 2
    assert recovered_broker.export_state().fills == ()
    assert (await recovered.step()).processed_sequences == (3,)


async def test_checkpoint_failure_retry_does_not_repeat_successful_side_effects(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    repository = InMemoryAuditRepository(demo_binding)
    fail = True

    async def persist(value: OfflineReplayCheckpoint) -> None:
        if fail:
            raise RuntimeError("checkpoint unavailable")
        await recorder.checkpoint(value)

    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        repository,
        persist,
        event_handler=recorder.event,
        quote_consumer=recorder.quote,
    )
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await session.step()
    assert session.checkpoint.last_sequence is None
    assert len(recorder.events) == 1
    fail = False
    assert (await session.step()).processed_sequences == (0,)
    assert len(recorder.events) == 1
    assert len(await repository.list("market_snapshots")) == 1
    assert len(await repository.list("sentinel_events")) == 1
    fail = True
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        await session.step()
    assert len(recorder.quotes) == 2
    fail = False
    assert (await session.step()).processed_sequences == (1, 2)
    assert len(recorder.quotes) == 2


async def test_handler_failure_retries_same_stable_event_without_leaking_bus_capacity(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    seen: list[SentinelEvent] = []

    async def handle(event: SentinelEvent) -> None:
        seen.append(event)
        if len(seen) == 1:
            raise RuntimeError("handler unavailable")

    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
        event_handler=handle,
        event_bus_maxsize=1,
    )
    with pytest.raises(RuntimeError, match="handler unavailable"):
        await session.step()
    assert session.checkpoint.last_sequence is None
    await session.step()
    assert seen[0] == seen[1]
    await session.step()
    assert session.event_bus.pending == 0
    assert session.event_bus.dropped_events == 0


async def test_repository_failure_precedes_callbacks_and_can_be_retried(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    class FailingRepository(InMemoryAuditRepository):
        fail_events = True

        async def append(self, table: str, value: BaseModel | Mapping[str, Any]) -> UUID:
            if table == "sentinel_events" and self.fail_events:
                raise RuntimeError("event persistence unavailable")
            return await super().append(table, value)

    recorder = Recorder()
    repository = FailingRepository(demo_binding)
    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        repository,
        recorder.checkpoint,
        event_handler=recorder.event,
    )
    with pytest.raises(RuntimeError, match="event persistence unavailable"):
        await session.step()
    assert recorder.events == [] and recorder.checkpoints == []
    repository.fail_events = False
    await session.step()
    assert len(await repository.list("market_snapshots")) == 1
    assert len(recorder.events) == 1


async def test_bus_overflow_is_bounded_and_retry_does_not_republish_delivered_events(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
        event_bus_maxsize=1,
    )
    await session.step()
    with pytest.raises(RuntimeError, match="event bus is full"):
        await session.step()
    assert session.checkpoint.last_sequence == 0
    first = await session.event_bus.receive()
    session.event_bus.task_done()
    assert first.payload["replay_sequence"] == 0
    with pytest.raises(RuntimeError, match="event bus is full"):
        await session.step()
    second = await session.event_bus.receive()
    session.event_bus.task_done()
    assert second.payload["replay_sequence"] == 1
    assert (await session.step()).processed_sequences == (1, 2)
    third = await session.event_bus.receive()
    session.event_bus.task_done()
    assert third.payload["replay_sequence"] == 2


async def test_duplicate_quote_snapshot_consumed_once_and_missing_ids_are_stable(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    first_quote = fixture.records[1]
    records = (
        first_quote,
        first_quote.model_copy(update={"sequence": 2}),
    )
    duplicate_fixture = fixture.model_copy(update={"records": records})
    recorder = Recorder()
    session = make_session(duplicate_fixture, demo_binding, recorder)
    await session.step()
    assert len(recorder.quotes) == 1
    assert len(recorder.events) == 2
    assert recorder.events[0].deduplication_key != recorder.events[1].deduplication_key
    payload = dict(first_quote.payload)
    payload.pop("snapshot_id")
    missing_id_fixture = fixture.model_copy(
        update={"records": (first_quote.model_copy(update={"payload": payload}),)}
    )
    other = Recorder()
    one = make_session(missing_id_fixture, demo_binding, recorder)
    two = make_session(missing_id_fixture, demo_binding, other)
    one_result = await one.step()
    two_result = await two.step()
    assert one_result.option_quotes == two_result.option_quotes
    assert one_result.events == two_result.events
    assert (await one.market.get_option_chain("ACME"))[0] == one_result.option_quotes[0]


async def test_concurrent_steps_serialize_without_duplicate_records(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    results = await asyncio.gather(session.step(), session.step(), session.step())
    assert [item.processed_sequences for item in results] == [(0,), (1, 2), (3,)]
    assert len(recorder.events) == 4


async def test_empty_fixture_persists_completion_once(demo_binding: RuntimeBinding) -> None:
    fixture = ReplayFixture(
        version="empty-v1", provider="offline", capability_version="v1", records=()
    )
    recorder = Recorder()
    session = OfflineReplaySession(
        fixture,
        VirtualClock(datetime(2026, 1, 5, tzinfo=UTC)),
        InMemoryAuditRepository(demo_binding),
        recorder.checkpoint,
    )
    assert not session.complete
    assert (await session.step()).checkpoint.complete
    assert (await session.step()).processed_sequences == ()
    assert len(recorder.checkpoints) == 1


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"fixture_hash": "0" * 64}, "fixture_hash"),
        ({"namespace": "other"}, "namespace"),
        ({"provider": "other"}, "provider"),
        ({"last_sequence": 99}, "sequence is not in fixture"),
        ({"last_sequence": 1}, "splits a timestamp group"),
        ({"complete": True}, "completion does not match"),
        ({"replay_time": "2026-01-05T14:30:00Z"}, "time does not match sequence"),
    ],
)
async def test_restore_rejects_inconsistent_checkpoint(
    fixture: ReplayFixture,
    demo_binding: RuntimeBinding,
    changes: dict[str, Any],
    reason: str,
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    await session.step()
    await session.step()
    saved = OfflineReplayCheckpoint.model_validate({**session.checkpoint.model_dump(), **changes})
    with pytest.raises(DataInvalidError, match=reason):
        OfflineReplaySession(
            fixture,
            VirtualClock(saved.replay_time),
            InMemoryAuditRepository(demo_binding),
            recorder.checkpoint,
            checkpoint=saved,
        )


async def test_restore_rejects_clock_before_checkpoint_or_after_next_group(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    await session.step()
    for clock_time in (
        session.checkpoint.replay_time - timedelta(seconds=1),
        fixture.records[3].available_at,
    ):
        with pytest.raises(DataInvalidError, match="clock"):
            OfflineReplaySession(
                fixture,
                VirtualClock(clock_time),
                InMemoryAuditRepository(demo_binding),
                recorder.checkpoint,
                checkpoint=session.checkpoint,
            )


def test_oversized_group_and_conflicting_snapshot_identity_fail_before_side_effects(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    with pytest.raises(DataInvalidError, match="exceeds max_records_per_step"):
        OfflineReplaySession(
            fixture,
            VirtualClock(fixture.records[0].available_at),
            InMemoryAuditRepository(demo_binding),
            recorder.checkpoint,
            max_records_per_step=1,
        )
    quote = fixture.records[1]
    conflict = quote.model_copy(update={"sequence": 2, "payload": {**quote.payload, "ask": "0.10"}})
    with pytest.raises(DataInvalidError, match="conflicting payloads"):
        make_session(
            fixture.model_copy(update={"records": (quote, conflict)}), demo_binding, recorder
        )
    assert recorder.checkpoints == []


async def test_delayed_observation_does_not_regress_freshness(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    first = fixture.records[0]
    effective_at = first.effective_at - timedelta(seconds=10)
    observed_at = first.observed_at + timedelta(seconds=1)
    payload = {
        **first.payload,
        "snapshot_id": str(uuid4()),
        "metadata": {
            **first.payload["metadata"],
            "observed_at": observed_at.isoformat(),
            "effective_at": effective_at.isoformat(),
        },
    }
    delayed = first.model_copy(
        update={
            "sequence": 1,
            "observed_at": observed_at,
            "effective_at": effective_at,
            "payload": payload,
        }
    )
    recorder = Recorder()
    session = make_session(
        fixture.model_copy(update={"records": (first, delayed)}), demo_binding, recorder
    )
    await session.step()
    await session.step()
    assert session.freshness.age("equity_quote:ACME") == timedelta(seconds=1)
    assert recorder.events[-1].effective_at == effective_at


async def test_bars_persist_as_market_snapshots_and_restore_completed_replay(
    demo_binding: RuntimeBinding,
) -> None:
    fixture = ReplayFixture.model_validate_json(bundled_fixture_path().read_text(encoding="utf-8"))
    recorder = Recorder()
    repository = InMemoryAuditRepository(demo_binding)
    session = OfflineReplaySession(
        fixture,
        VirtualClock(fixture.records[0].available_at),
        repository,
        recorder.checkpoint,
        event_handler=recorder.event,
    )
    for _ in fixture.records:
        await session.step()
        if session.complete:
            break
    assert session.complete
    bars = [
        row["payload"]
        for row in await repository.list("market_snapshots")
        if row["payload"]["replay_kind"] == "bar"
    ]
    assert bars
    assert all(item["data_mode"] == "REPLAY" for item in bars)
    assert isinstance(bars[0]["close"], str)
    restored = OfflineReplaySession(
        fixture,
        VirtualClock(session.checkpoint.replay_time),
        repository,
        recorder.checkpoint,
        checkpoint=session.checkpoint,
    )
    assert restored.complete
    assert (await restored.step()).processed_sequences == ()
    with pytest.raises(DataInvalidError, match="completed replay clock"):
        OfflineReplaySession(
            fixture,
            VirtualClock(session.checkpoint.replay_time + timedelta(seconds=1)),
            repository,
            recorder.checkpoint,
            checkpoint=session.checkpoint,
        )


async def test_external_clock_advance_fails_closed(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    session = make_session(fixture, demo_binding, recorder)
    await session.clock.advance_to(fixture.records[-1].available_at)
    with pytest.raises(DataInvalidError, match="clock passed"):
        await session.step()
    assert recorder.events == [] and recorder.checkpoints == []


async def test_wrong_environment_or_namespace_rejected_before_shared_writes(
    fixture: ReplayFixture, demo_binding: RuntimeBinding
) -> None:
    recorder = Recorder()
    for changes in (
        {"environment": ExecutionEnvironment.LIVE, "demo_backend": None},
        {"demo_backend": DemoBackend.BROKER_SHADOW},
        {"external_write_authority": True},
    ):
        repository = InMemoryAuditRepository(demo_binding.model_copy(update=changes))
        with pytest.raises(SafetyCriticalError, match="DEMO/OFFLINE_SIM"):
            OfflineReplaySession(
                fixture,
                VirtualClock(fixture.records[0].available_at),
                repository,
                recorder.checkpoint,
            )
        assert await repository.list("market_snapshots") == []
    with pytest.raises(SafetyCriticalError, match="namespace"):
        OfflineReplaySession(
            fixture,
            VirtualClock(fixture.records[0].available_at),
            InMemoryAuditRepository(demo_binding),
            recorder.checkpoint,
            namespace="other-namespace",
        )
